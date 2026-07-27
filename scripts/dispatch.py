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
    payload = {
        "task_type": h.get("task_type", "implement"),
        "action": "advise",
        "trigger": "dispatch",
        "ticket": t.get("number"),
        "handoff": h,
    }
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
