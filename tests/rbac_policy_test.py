#!/usr/bin/env python3
"""Assert the router's approver-RBAC policy is well-formed (threat-model M5).

Runnable without Node. The core invariant rbac.js relies on: EVERY risk level
maps to >=1 authorized approver group (rbac.js fails closed on an unmapped
level). Also sanity-checks that byRisk/byScope keys are real and group lists are
non-empty strings.

    python tests/rbac_policy_test.py     # exits non-zero on any failure
"""
import os, sys, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "router", "policy.json"), encoding="utf-8") as f:
    P = json.load(f)

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)

def group_list_ok(v):
    return isinstance(v, list) and len(v) >= 1 and all(isinstance(g, str) and g for g in v)

levels = P.get("risk", {}).get("levels", [])
appr = P.get("approvers")

check("approvers section present", isinstance(appr, dict))
appr = appr or {}
by_risk = appr.get("byRisk", {})
check("approvers.byRisk present", isinstance(by_risk, dict))

print("Every risk level maps to >=1 approver group:")
for lvl in levels:
    check(f'risk level "{lvl}" maps to >=1 group', group_list_ok(by_risk.get(lvl)), repr(by_risk.get(lvl)))

print("byRisk keys are real risk levels:")
for k in by_risk:
    if k.startswith("_"):
        continue
    check(f'byRisk key "{k}" is a real risk level', k in levels)

print("Segregation-of-duties references real levels:")
sod = appr.get("segregationOfDuties", {}).get("riskLevels", [])
check("SoD riskLevels is a list", isinstance(sod, list))
for lvl in sod:
    check(f'SoD level "{lvl}" is a real risk level', lvl in levels)

print("byScope entries (if any) are non-empty group lists:")
for k, v in (appr.get("byScope") or {}).items():
    if k.startswith("_"):
        continue
    check(f'byScope["{k}"] is a non-empty group list', group_list_ok(v), repr(v))

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}"); sys.exit(1)
print("All approver-RBAC policy checks passed.")
