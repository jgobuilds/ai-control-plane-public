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
