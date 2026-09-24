#!/usr/bin/env python3
"""Assign one ticket to one agent, and build the handoff. Single writer.

Implements [ADR 0012](../docs/decisions/0012-work-dispatch.md).

THE WHOLE DESIGN IN ONE LINE: **one dispatcher assigns; agents never self-claim.**
A single writer of ticket ownership cannot race itself, which is why there is no
compare-and-set here and no lease — and why ADR 0010's N2 stopped blocking. This
is the chokepoint argument that produced the router ("n8n calls the router, not
the runners"), applied one level out to work assignment.

WHY NOT A BUS. Publishing "ticket 12 needs work" to N agents gets it done N
times unless exclusive delivery is added — at which point the bus is a queue, and
the ticket store already is one. Duplicate execution is a CORRECTNESS problem
(conflicting commits, double-posts, a rate limit spent twice); bounded throughput
is a throughput problem, recoverable by restarting a lane. We take the second.

WHAT IT DOES NOT DO. It does not execute anything. It picks work, writes the
handoff, and hands off to the router — the only thing that reaches a runner.
It cannot run an agent, and adding that here would put a second authority beside
the router, which is the open C2 finding in the threat model.

    python scripts/dispatch.py --dry-run          # what would be assigned
    python scripts/dispatch.py --label agent-ready
    python scripts/dispatch.py --json             # for the lanes endpoint

Pure stdlib.
"""
from __future__ import annotations
import argparse, importlib.util, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))

# The loop guard from AGENT-DESIGN.md §5. A handoff chain that exceeds this has
# stopped converging: A→B→C→A because nobody owns the task. The ceiling is the
# mitigation, and it is checked here rather than trusted to each agent.
MAX_HOPS = 5

# A ticket already assigned is NOT claimable. This is the entire mutual-exclusion
# mechanism, and it is sound only because one process does the assigning.
READY_LABEL = "agent-ready"


ROOT = os.path.dirname(HERE)
# The router's policy is the ONE source for which verbs, triggers and task types
# exist. It is mounted read-only into the lanes container at the same relative
# path (docker-compose.yml), so this resolves in the repo and in the container.
POLICY_PATH = os.environ.get("AICP_POLICY_PATH") or os.path.join(ROOT, "router", "policy.json")

# Dispatch is polled by a timer, so the honest trigger is "scheduled". It used to
# send "dispatch", which the policy does not define: under warn that scored as a
# human turn, and under block it is refused.
TRIGGER = "scheduled"
# Work whose result is a change to files. A handoff for any other task type only
# reads and reports, unless the handoff states a verb itself.
WRITE_TASKS = {"code-edit", "generate", "transform", "debug", "test-design"}
# The pre-contract default. It was never a task type the router knew.
LEGACY_TASK = {"implement": "code-edit"}


def load_policy(path=None):
    """The router policy, or None when it cannot be read. Callers must fail closed
    on None: guessing the verb list is how a lane ends up under-declaring."""
    try:
        with open(path or POLICY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def build_prompt(h, number):
    """The briefing the receiving agent actually reads. AGENT-DESIGN §5: written
    for the receiver, who has no history; `acceptance` explicit, because
    "lost context" is the documented failure mode."""
    parts = [
        f"You have been assigned ticket #{number}. You have no prior context beyond this briefing.",
        "",
        "OBJECTIVE:",
        str(h.get("objective", "")).strip(),
        "",
        "ACCEPTANCE (how you know you are done; check it yourself before finishing):",
        str(h.get("acceptance", "")).strip(),
    ]
    ctx = h.get("context")
    if ctx:
        parts += ["", "CONTEXT:", ctx if isinstance(ctx, str) else json.dumps(ctx, indent=2, ensure_ascii=False)]
    parts += ["", f"You own this ticket (owner: {h.get('owner')}, hop {h.get('hops')}). "
              "If you cannot meet the acceptance criterion, say so plainly and why. "
              "Do not claim success you have not checked."]
    return "\n".join(parts)


def router_payload(h, number, policy):
    """The /route body for a handoff, or (None, reason) when it cannot be built
    honestly. Every value is one the router's policy defines. An unknown one
    blocks the ticket rather than being guessed, because a guessed verb is an
    under-declared one (threat-model H5)."""
    if policy is None:
        return None, f"cannot read the router policy at {POLICY_PATH}; refusing to guess what the work may do"
    verbs = {k for k, v in (policy.get("risk", {}).get("access") or {}).items()
             if k != "default" and isinstance(v, int)}
    tasks = set(policy.get("taskTypes") or {})

    task = h.get("task_type") or "implement"
    task = LEGACY_TASK.get(task, task)
    if task not in tasks:
        return None, f"task_type {task!r} is not one the router knows ({', '.join(sorted(tasks))})"

    action = h.get("action") or ("write" if task in WRITE_TASKS else "advise")
    if action not in verbs:
        return None, f"action {action!r} is not a router verb ({', '.join(sorted(verbs))})"

    payload = {
        "prompt": build_prompt(h, number),
        # The router hands `goal` to its fresh-context verifier. The acceptance
        # criterion is exactly what that reviewer should judge against.
        "goal": f"{h.get('objective', '')}\nAcceptance: {h.get('acceptance', '')}",
        "task_type": task,
        "action": action,
        "trigger": TRIGGER,
        "ticket": number,
        "handoff": h,
    }
    if h.get("scope"):
        payload["scope"] = h["scope"]
    return payload, None


def _ticket_mod():
    spec = importlib.util.spec_from_file_location(
        "ticket", os.path.join(HERE, "ticket.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def parse_handoff(body):
    """The handoff payload embedded in a ticket body, or {}.

    The contract is AGENT-DESIGN.md §5 and is NOT redefined here — a second
    definition of a payload shape is how two agents end up disagreeing about
    what `acceptance` means.
    """
    if not body:
        return {}
    marker = "```handoff"
    if marker not in body:
        return {}
    try:
        block = body.split(marker, 1)[1].split("```", 1)[0]
        return json.loads(block)
    except (ValueError, IndexError):
        return {}


def render_handoff(h):
    return "```handoff\n" + json.dumps(h, indent=2, ensure_ascii=False) + "\n```"


def validate(h):
    """Missing required fields, and whether the hop ceiling is blown.

    `acceptance` is required and its absence is a hard stop, not a warning:
    AGENT-DESIGN names "lost context" as a documented failure mode, mitigated by
    making acceptance explicit rather than trusting shared understanding. A
    handoff with no acceptance criterion cannot be verified by the receiver, so
    dispatching it just moves the ambiguity onto someone else.
    """
    missing = [f for f in ("objective", "acceptance") if not h.get(f)]
    hops = int(h.get("hops") or 0)
    return missing, hops


def select(tickets, exclude_assigned=True):
    """The one ticket to dispatch. Oldest first — a queue, not a lottery.

    Deterministic on purpose: a dispatcher that picks randomly cannot be
    reasoned about when something goes wrong, and "why did it do that one?" is
    the first question anyone asks.
    """
    claimable = [t for t in tickets
                 if not (exclude_assigned and t.get("assignees"))]
    return claimable[0] if claimable else None


def dispatch(backend, label=READY_LABEL, dry_run=False, assignee=None):
    # Oldest-first ordering is the BACKEND's contract (see ticket.py), not a
    # guess made here. The first version reversed the list on the assumption
    # that every backend returned newest-first — true for GitHub, false for
    # file, so it served the wrong end of the queue in one of them. When a
    # caller has to guess an interface, the interface is the bug.
    tickets = backend.list(label)
    if not tickets:
        return {"dispatched": None,
                "note": f"no open tickets labelled {label!r} — nothing to do, "
                        f"which is the normal state, not a failure"}

    t = select(tickets)
    if not t:
        return {"dispatched": None,
                "note": f"{len(tickets)} ticket(s) labelled {label!r}, all "
                        f"already assigned. Assignment IS the claim, so this "
                        f"means the fleet is busy, not that something is wrong"}

    h = parse_handoff(t.get("body", ""))
    missing, hops = validate(h)

    if missing:
        return {"dispatched": None, "blocked": t.get("number"),
                "note": f"ticket #{t.get('number')} has no {', '.join(missing)} "
                        f"in its handoff block. NOT dispatching: a handoff the "
                        f"receiver cannot verify moves the ambiguity rather than "
                        f"resolving it (AGENT-DESIGN §5, 'lost context')."}
    if hops >= MAX_HOPS:
        return {"dispatched": None, "blocked": t.get("number"),
                "note": f"ticket #{t.get('number')} is at hop {hops} of "
                        f"{MAX_HOPS}. NOT dispatching: this chain has stopped "
                        f"converging and needs a human, not another agent."}

    h = dict(h, hops=hops + 1, owner=assignee or h.get("owner") or "unassigned")
    # Built BEFORE claiming: a ticket the router would refuse must not be taken
    # off the queue, or it sits claimed with nobody working on it.
    payload, why = router_payload(h, t.get("number"), load_policy())
    if payload is None:
        return {"dispatched": None, "blocked": t.get("number"),
                "note": f"ticket #{t.get('number')}: {why}. NOT dispatching — fix "
                        f"the handoff block; the router would refuse this request."}
    if dry_run:
        return {"dispatched": t.get("number"), "dry_run": True,
                "payload": payload, "note": "[dry-run] nothing was assigned"}

    # ACTUALLY CLAIM IT. The first version built the payload, returned
    # "assigned #N", and never called assign() — so every poll would have
    # re-dispatched the same ticket forever while reporting success. The claim
    # is not a side effect of dispatching; it IS the mutual exclusion, and
    # "I ran the dispatcher" is not "the ticket was claimed".
    owner = assignee or h.get("owner")
    if not owner or owner == "unassigned":
        return {"dispatched": None, "blocked": t.get("number"),
                "note": f"ticket #{t.get('number')} is ready but no agent was "
                        f"named to take it (--assignee). NOT dispatching: an "
                        f"unclaimed ticket would be re-served on every poll."}
    ok, detail = backend.assign(t.get("key"), owner)
    if not ok:
        # Fail loudly rather than hand the work out unclaimed. Unclaimed
        # dispatch IS the duplicate-execution failure this design exists to
        # prevent, and it would look exactly like success.
        return {"dispatched": None, "blocked": t.get("number"),
                "note": f"could not claim #{t.get('number')}: {detail}. NOT "
                        f"dispatching — handing out unclaimed work is the "
                        f"duplicate-execution failure this design prevents."}
    return {"dispatched": t.get("number"), "payload": payload, "owner": owner,
            "note": f"claimed #{t.get('number')} for {owner}, hop {h['hops']}"}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Assign one ticket (single writer).")
    ap.add_argument("--label", default=READY_LABEL)
    ap.add_argument("--backend", default=None)
    ap.add_argument("--repo", default=None)
    ap.add_argument("--assignee", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args(argv)

    t = _ticket_mod()
    b = t.get_backend(a.backend, a.repo)
    ok, why = b.available()
    if not ok:
        out = {"dispatched": None, "error": f"backend {b.name!r} unavailable: {why}"}
        print(json.dumps(out) if a.as_json else out["error"], file=sys.stderr)
        return 2

    res = dispatch(b, a.label, a.dry_run, a.assignee)
    if a.as_json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(f"  {res.get('note')}")
        if res.get("payload"):
            print(f"  -> POST /route  ticket #{res['payload']['ticket']}  "
                  f"hop {res['payload']['handoff']['hops']}")
    # Always 0. "Nothing to dispatch" is the normal state of a healthy queue, and
    # a lane that goes red on an empty queue trains people to ignore it.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
