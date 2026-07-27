#!/usr/bin/env python3
"""Kill switch tests — runnable without Node.

Drives scripts/halt.py against a TEMP control dir (never the real control/) and
asserts the halting semantics the router relies on: a global halt stops
everything; a scope halt stops that scope; and an ANCESTOR halt stops a
descendant (router/killswitch.js matches halt.json against the request's full
scope CHAIN). is_halted() below mirrors killswitch.js isHalted().

    python tests/killswitch_test.py     # exits non-zero on any failure
"""
import os, sys, json, subprocess, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HALT_PY = os.path.join(ROOT, "scripts", "halt.py")

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)

def run(control_dir, *args):
    env = dict(os.environ, CONTROL_DIR=control_dir)
    return subprocess.run([sys.executable, HALT_PY, *args], env=env, capture_output=True, text=True)

def read_state(control_dir):
    p = os.path.join(control_dir, "halt.json")
    if not os.path.exists(p):
        return {"global": False, "scopes": [], "reason": "", "ts": ""}
    with open(p, encoding="utf-8") as f:
        return json.load(f)

# Mirror of router/killswitch.js isHalted(state, scopeChain).
def is_halted(state, chain):
    if state.get("global"):
        return True
    listed = set(state.get("scopes", []))
    return any(sc in listed for sc in chain)

# Scope chains (root -> node), matching context/scopes.json.
CHAIN = {
    "client-a":   ["enterprise", "consulting", "client-a"],
    "jane":       ["enterprise", "engineering", "data-team", "jane"],
    "consulting": ["enterprise", "consulting"],
}

with tempfile.TemporaryDirectory() as d:
    # 1. global halt stops everything
    run(d, "global", "incident-42")
    st = read_state(d)
    check("global halt: file flag set", st["global"] is True)
    check("global halt: reason recorded", st["reason"] == "incident-42")
    check("global halt: ts recorded", bool(st["ts"]))
    check("global halt: halts client-a", is_halted(st, CHAIN["client-a"]))
    check("global halt: halts jane", is_halted(st, CHAIN["jane"]))

    run(d, "resume", "global")
    st = read_state(d)
    check("resume global: nothing halted", not is_halted(st, CHAIN["client-a"]))

    # 2. scope halt stops just that scope
    run(d, "scope", "client-a", "leak review")
    st = read_state(d)
    check("scope halt: client-a listed", "client-a" in st["scopes"])
    check("scope halt: halts client-a", is_halted(st, CHAIN["client-a"]))
    check("scope halt: does NOT halt jane", not is_halted(st, CHAIN["jane"]))

    # 3. ancestor halt stops a descendant (chain match)
    run(d, "resume")  # clear first
    run(d, "scope", "consulting", "vendor pause")
    st = read_state(d)
    check("ancestor halt: consulting listed", "consulting" in st["scopes"])
    check("ancestor halt: halts descendant client-a", is_halted(st, CHAIN["client-a"]))
    check("ancestor halt: does NOT halt jane", not is_halted(st, CHAIN["jane"]))

    # 4. resume all clears everything
    run(d, "resume")
    st = read_state(d)
    check("resume all: not global", not st["global"])
    check("resume all: no scopes", st["scopes"] == [])
    check("status command exits 0", run(d, "status").returncode == 0)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("All kill switch checks passed.")
