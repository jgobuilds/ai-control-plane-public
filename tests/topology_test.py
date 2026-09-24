#!/usr/bin/env python3
"""Prove the conformance gate FAILS on a malformed scope tree instead of crashing.

WHY THIS EXISTS. Issue #7 ("CI: unrecognised failure in quality", 2026-07-31) was
not really an unknown failure. conformance_test.py raised KeyError partway through
the C1 mount proof, so the log held a traceback rather than a verdict — ci_diagnose
matched no signature, escalated, and filed a ticket for a crash nobody could read
from the outside. The crash was fixed 2.5 minutes later; the CLASS was not.

The class matters more than that instance. A gate that crashes has not failed and
has not passed: every check after the crash point never ran, and a partial log of
PASS lines reads exactly like a clean one. That is the vacuity doctrine applied to
the gate itself — an unrun check is `unknown`, and the gate has to say so.

Three malformed inputs, all confirmed reachable against the naive ancestry walk:

    unknown scope name    KeyError   — issue #7's own shape
    dangling parent       KeyError   — delete a scope, leave its children pointing
    parent cycle          NO ERROR   — an unbounded loop. Worst of the three: the
                                       job hangs to the runner timeout with no text
                                       to diagnose. This case is why the test has a
                                       timeout rather than just checking a message.

Each case demands: exit 1, a named FAIL, and NO traceback. The last assertion is
the point — "it exited non-zero" is satisfied by a crash too.

    python tests/topology_test.py
"""
import json, os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "scripts", "conformance_test.py")

# A minimal WELL-FORMED tree, used as the base each case then breaks in one way.
# Kept tiny on purpose: the subject under test is ancestry resolution, and extra
# scopes would only add ways for a case to fail for a reason other than its own.
BASE = {
    "nodes": {
        "acme":  {"level": "client", "controls": {"isolation": "silo"}},
        "alpha": {"level": "engagement", "parent": "acme"},
    },
    "levelDefaults": {"client": {}, "engagement": {}},
    "conflicts": {"pairs": []},
}


def overlay(tmp, nodes):
    """Write a throwaway *-instance overlay the gate will resolve to via AICP_INSTANCE.

    The generated artifacts have to exist too — the gate loads runner-map.json and
    compose.scopes.yml before it reaches the topology check — but they are allowed
    to be empty here. If the precondition works, nothing downstream of it runs.
    """
    inst = os.path.join(tmp, "probe-instance")
    os.makedirs(os.path.join(inst, "context"), exist_ok=True)
    gen = os.path.join(inst, "generated")
    os.makedirs(os.path.join(gen, "context"), exist_ok=True)
    tree = dict(BASE, nodes=nodes)
    json.dump(tree, open(os.path.join(inst, "context", "scopes.json"), "w", encoding="utf-8"))
    json.dump({"scopeToHost": {}, "providerHosts": {}, "roots": []},
              open(os.path.join(gen, "context", "runner-map.json"), "w", encoding="utf-8"))
    open(os.path.join(gen, "compose.scopes.yml"), "w", encoding="utf-8").write("services: {}\n")
    return inst


def run(inst):
    env = dict(os.environ, AICP_INSTANCE=inst, PYTHONIOENCODING="utf-8")
    # The timeout is load-bearing, not hygiene: the cycle case does not raise, it
    # loops. Without a timeout this test would hang exactly the way CI did.
    p = subprocess.run([sys.executable, GATE], cwd=ROOT, env=env, timeout=60,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


CASES = [
    ("unknown scope name (issue #7's shape)",
     {"acme": {"level": "client", "controls": {"isolation": "silo"}},
      "alpha": {"level": "engagement", "parent": "acme"},
      "ghost": {"level": "engagement", "parent": "nowhere"}}),
    ("dangling parent (parent scope deleted)",
     {"alpha": {"level": "engagement", "parent": "acme"}}),
    ("parent cycle a -> b -> a (loops rather than raising)",
     {"a": {"level": "engagement", "parent": "b"},
      "b": {"level": "engagement", "parent": "a"}}),
]

fails = []


def expect(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def main():
    tmp = tempfile.mkdtemp(prefix="topology-test-")
    try:
        print("A malformed scope tree fails the gate cleanly (never crashes, never hangs):")
        for label, nodes in CASES:
            try:
                code, out = run(overlay(tmp, nodes))
            except subprocess.TimeoutExpired:
                expect(f"{label}: terminates", False, "still running after 60s -- unbounded loop")
                continue
            expect(f"{label}: exits non-zero", code != 0, f"exit {code}")
            expect(f"{label}: reports the precondition as FAILED",
                   "malformed scope topology" in out, out.strip().splitlines()[-1][:120] if out.strip() else "(no output)")
            # The one that separates a real failure from a crash. Everything above
            # is equally true of a traceback.
            expect(f"{label}: does NOT crash (no traceback)",
                   "Traceback (most recent call last)" not in out,
                   "the gate raised instead of failing -- this is issue #7 again")
            expect(f"{label}: says the C1 proof did not run",
                   "was NOT evaluated" in out or "UNKNOWN, not passing" in out,
                   "an unrun check must not read as a passing one")

        print("\nA well-formed tree still passes the precondition:")
        code, out = run(overlay(tmp, BASE["nodes"]))
        expect("well-formed tree: precondition passes",
               "PASS  every scope's ancestry resolves to a root" in out, out[:200])
        expect("well-formed tree: not stopped by the precondition",
               "malformed scope topology" not in out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
        return 1
    print("All topology checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
