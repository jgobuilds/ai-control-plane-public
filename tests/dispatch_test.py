#!/usr/bin/env python3
"""Corpus for the work dispatcher (ADR 0012).

One property matters more than everything else: **the same ticket must never be
served twice.** Duplicate execution is a correctness failure — two agents on one
ticket produce conflicting commits, double-posts, or a rate limit spent twice.
Bounded throughput is merely slow, and recoverable by restarting a lane.

The rest of these exist because each was got wrong on the way here.

    python tests/dispatch_test.py
"""
import importlib.util, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, "..", "scripts", name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


d = _load("dispatch")
t = _load("ticket")

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def fresh():
    return t.FileBackend(os.path.join(tempfile.mkdtemp(), "t.jsonl"))


def handoff(**kw):
    h = {"objective": "o", "acceptance": "a", "hops": 0}
    h.update(kw)
    return "```handoff\n" + json.dumps(h) + "\n```"


print("The same ticket is never served twice:")
b = fresh()
for i in (1, 2):
    b.open(f"w/{i}", f"work {i}", handoff(), ["agent-ready"])
first = d.dispatch(b, assignee="agent-1")
second = d.dispatch(b, assignee="agent-2")
check("two dispatches serve two different tickets",
      first["dispatched"] == 1 and second["dispatched"] == 2,
      f"{first['dispatched']} then {second['dispatched']}")
third = d.dispatch(b, assignee="agent-3")
check("a fully claimed queue serves nothing", third["dispatched"] is None)
check("and says the fleet is busy rather than implying a fault",
      "busy" in third["note"])

print("\nThe claim actually lands — 'dispatched' is not 'claimed':")
# The first version built the payload, returned "assigned #N", and never called
# assign(). Every poll would have re-served the same ticket while reporting
# success — the exact duplicate-execution failure the design exists to prevent.
b = fresh()
b.open("w/1", "work", handoff(), ["agent-ready"])
d.dispatch(b, assignee="agent-1")
check("the ticket is assigned in the STORE afterwards",
      b.list()[0]["assignees"] == ["agent-1"], str(b.list()[0].get("assignees")))
check("so a second poll cannot re-serve it",
      d.dispatch(b, assignee="agent-2")["dispatched"] is None)

print("\nUnclaimable work is refused, not handed out:")
b = fresh()
b.open("w/1", "work", handoff(), ["agent-ready"])
res = d.dispatch(b)                     # no assignee named
check("no assignee -> not dispatched", res["dispatched"] is None)
check("and it explains the re-serve risk", "re-served" in res["note"])


class _RefusingBackend(t.FileBackend):
    def assign(self, key, who):
        return False, "simulated store failure"


b = _RefusingBackend(os.path.join(tempfile.mkdtemp(), "t.jsonl"))
b.open("w/1", "work", handoff(), ["agent-ready"])
res = d.dispatch(b, assignee="agent-1")
check("a failed claim blocks the dispatch rather than proceeding unclaimed",
      res["dispatched"] is None and "could not claim" in res["note"])

print("\nA handoff the receiver cannot verify is not dispatched:")
b = fresh()
b.open("w/1", "no acceptance", "```handoff\n{\"objective\":\"x\",\"hops\":0}\n```",
       ["agent-ready"])
res = d.dispatch(b, assignee="a1")
check("missing acceptance blocks", res["dispatched"] is None)
check("it names the missing field", "acceptance" in res["note"])
check("and cites the failure mode rather than just refusing",
      "lost context" in res["note"])
b = fresh()
b.open("w/1", "no handoff at all", "just prose", ["agent-ready"])
check("a ticket with no handoff block blocks too",
      d.dispatch(b, assignee="a1")["dispatched"] is None)

print("\nThe hop ceiling stops a chain that has stopped converging:")
b = fresh()
b.open("w/1", "looping", handoff(hops=d.MAX_HOPS), ["agent-ready"])
res = d.dispatch(b, assignee="a1")
check("at the ceiling it refuses", res["dispatched"] is None)
check("and routes to a human, not another agent", "needs a human" in res["note"])
b = fresh()
b.open("w/1", "fine", handoff(hops=d.MAX_HOPS - 1), ["agent-ready"])
res = d.dispatch(b, assignee="a1")
check("one below the ceiling still dispatches", res["dispatched"] == 1)
check("and the hop counter increments", res["payload"]["handoff"]["hops"] == d.MAX_HOPS)

print("\nOrdering is the backend's contract, not the caller's guess:")
# The first version reversed the list, assuming every backend returned
# newest-first — true for GitHub, false for file. It served the wrong end of the
# queue in one of them.
b = fresh()
b.open("old", "oldest", handoff(), ["agent-ready"])
b.open("new", "newest", handoff(), ["agent-ready"])
res = d.dispatch(b, assignee="a1", dry_run=True)
check("the OLDEST claimable ticket is chosen", res["dispatched"] == 1,
      str(res["dispatched"]))

print("\nAn empty queue is the normal state of a healthy fleet:")
res = d.dispatch(fresh(), assignee="a1")
check("nothing to do is not an error", res["dispatched"] is None)
check("and it says so plainly", "normal state" in res["note"])

print("\nDry run touches nothing:")
b = fresh()
b.open("w/1", "work", handoff(), ["agent-ready"])
d.dispatch(b, assignee="a1", dry_run=True)
check("no assignment was written", b.list()[0]["assignees"] == [])

print("\nThe payload is something the router will actually accept:")
# Found 2026-09-18 while switching risk.enforce.actionBinding to "block". The
# payload had NO prompt, so the router answered 400 "prompt is required" to every
# dispatch. That means dispatch had never routed a ticket. It also declared
# action "advise" for implementation work (under block, advise gets read-only
# tools), a trigger the policy does not define ("dispatch"), and a task_type the
# policy does not know ("implement").
POLICY = json.load(open(os.path.join(HERE, "..", "router", "policy.json"), encoding="utf-8"))
VERBS = {k for k, v in POLICY["risk"]["access"].items() if k != "default" and isinstance(v, int)}
TRIGGERS = {k for k in POLICY["risk"]["autonomy"] if k != "default" and not k.startswith("_")}
TASKS = set(POLICY["taskTypes"])


def payload_for(**kw):
    b = fresh()
    b.open("w/1", "work", handoff(**kw), ["agent-ready"])
    return d.dispatch(b, assignee="a1", dry_run=True)


res = payload_for(objective="Rename the helper to parse_row", acceptance="tests/x_test.py passes",
                  context="Only scripts/x.py uses it")
pl = res.get("payload") or {}
check("the payload carries a prompt (the router rejects one without)",
      isinstance(pl.get("prompt"), str) and pl["prompt"].strip() != "")
check("the prompt is the briefing: objective, acceptance and context",
      all(s in pl.get("prompt", "") for s in ("Rename the helper to parse_row",
                                              "tests/x_test.py passes", "Only scripts/x.py uses it")))
check("the acceptance criterion reaches the router's verifier as `goal`",
      "tests/x_test.py passes" in str(pl.get("goal", "")))
check("the trigger is one the policy defines", pl.get("trigger") in TRIGGERS, repr(pl.get("trigger")))
check("dispatch is timer-driven, so the trigger is 'scheduled'", pl.get("trigger") == "scheduled")
check("the task_type is one the policy knows", pl.get("task_type") in TASKS, repr(pl.get("task_type")))
check("the legacy default 'implement' maps to 'code-edit'", pl.get("task_type") == "code-edit")
check("implementation work is declared as write, not advise", pl.get("action") == "write",
      repr(pl.get("action")))

pl = payload_for(task_type="summarize").get("payload") or {}
check("work that only reads and reports is declared advise",
      pl.get("task_type") == "summarize" and pl.get("action") == "advise", repr(pl))
pl = payload_for(task_type="review", action="read").get("payload") or {}
check("a handoff may state its own verb", pl.get("action") == "read")
check("every declared action is a policy verb", pl.get("action") in VERBS)

res = payload_for(action="deploy")
check("an unknown verb in the handoff blocks the ticket instead of guessing",
      res["dispatched"] is None and "deploy" in res.get("note", ""))
res = payload_for(task_type="make-it-work")
check("an unknown task_type blocks too, and names it",
      res["dispatched"] is None and "make-it-work" in res.get("note", ""))

pl = payload_for(scope="internal").get("payload") or {}
check("a scope named in the handoff is passed through", pl.get("scope") == "internal")
pl = payload_for().get("payload") or {}
check("no scope in the handoff leaves the router's default", "scope" not in pl)

print("\nThe lane reports what the router did with the ticket:")
# Nothing read the router's answer. A 400 failed the execution, and a 200 with
# `blocked` (approval required, verify unavailable) ended silently, with the
# ticket claimed and nobody told.
wf = json.load(open(os.path.join(HERE, "..", "n8n-workflows", "dispatch.workflow.json"), encoding="utf-8"))
route = [n for n in wf["nodes"] if "router:8080/route" in str(n["parameters"].get("url", ""))][0]
opts = route["parameters"].get("options", {})
check("the router call does not throw on a refusal, so the refusal can be read",
      (opts.get("response", {}).get("response", {}) or {}).get("neverError") is True, json.dumps(opts))
after = [o["node"] for outs in wf["connections"].get(route["name"], {}).get("main", []) for o in outs]
check("something reads the router's answer", len(after) > 0, repr(after))

print("\nThe lane can read the policy it has to match:")
import yaml
lanes = yaml.safe_load(open(os.path.join(HERE, "..", "docker-compose.yml"), encoding="utf-8"))["services"]["lanes"]
check("lanes mounts router/policy.json read-only",
      any(str(v).startswith("./router/policy.json:") and str(v).endswith(":ro") for v in lanes.get("volumes", [])),
      repr(lanes.get("volumes")))

print("\nIt cannot execute anything — only the router reaches a runner:")
import ast
_src = open(os.path.join(HERE, "..", "scripts", "dispatch.py"), encoding="utf-8").read()
_imports = set()
for n in ast.walk(ast.parse(_src)):
    if isinstance(n, ast.Import):
        _imports.update(a.name.split(".")[0] for a in n.names)
    elif isinstance(n, ast.ImportFrom) and n.module:
        _imports.add(n.module.split(".")[0])
for mod in ("subprocess", "urllib", "requests", "socket"):
    check(f"does not import {mod}", mod not in _imports, f"imports: {sorted(_imports)}")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All dispatcher checks passed.")
