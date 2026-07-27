#!/usr/bin/env python3
"""Conformity tests for the AI use-case register.

Guards the register's integrity WITHOUT touching conformance_test.py:
  - every scope marked status:"in-production" has a non-empty `owner` AND a
    resolvable operating mode (its own controls.mode or modes.default);
  - every `status` present is drawn from the allowed set;
  - the generator runs clean and produces BOTH output files.

    python tests/usecase_test.py      # exits non-zero on any failure
"""
import os, sys, json, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import gen_usecase_register as gen  # noqa: E402

STATUSES = {"proposed", "approved", "in-production", "retired"}

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)

def load(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return json.load(f)

S = load("context/scopes.json")
POLICY = load("router/policy.json")
ORDER = POLICY["modes"]["order"]

print("Status values are from the allowed set:")
for name, node in S["nodes"].items():
    st = node.get("status")
    if st is None:
        continue
    check(f"{name} status '{st}' allowed", st in STATUSES, f"not in {sorted(STATUSES)}")

print("In-production scopes have an owner and a resolvable operating mode:")
in_prod = [n for n, node in S["nodes"].items() if node.get("status") == "in-production"]
check("at least one in-production scope exists", len(in_prod) > 0, "none found")
for name in in_prod:
    node = S["nodes"][name]
    owner = node.get("owner")
    check(f"{name} has non-empty owner", bool(owner and str(owner).strip()), repr(owner))
    ctl = gen.effective_controls(S, name)
    mode = gen.effective_mode(POLICY, ctl)
    check(f"{name} has resolvable mode", mode in ORDER, repr(mode))

print("Generator runs and produces both output files:")
md, html = os.path.join(ROOT, "docs", "usecase-register.md"), os.path.join(ROOT, "usecase-register.html")
for p in (md, html):
    try:
        os.remove(p)
    except FileNotFoundError:
        pass
proc = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "gen_usecase_register.py")],
                      capture_output=True, text=True)
check("generator exits 0", proc.returncode == 0, proc.stderr.strip())
check("docs/usecase-register.md produced", os.path.isfile(md) and os.path.getsize(md) > 0)
check("usecase-register.html produced", os.path.isfile(html) and os.path.getsize(html) > 0)
if os.path.isfile(html):
    with open(html, encoding="utf-8") as f:
        body = f.read()
    check("html is self-contained (no external http resources)",
          "http://" not in body and "https://" not in body and "src=" not in body)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("All use-case register checks passed.")
