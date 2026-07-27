#!/usr/bin/env python3
"""Governance conformance tests — the invariants ARE the product, so they get
regression protection. Runs against the source-of-truth artifacts (scopes.json,
policy.json, generated compose.scopes.yml + runner-map.json). Pure stdlib+yaml.

    python scripts/conformance_test.py     # exits non-zero on any failure
"""
import os, sys, json, yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def load(p):
    with open(os.path.join(ROOT, p), encoding="utf-8") as f:
        return yaml.safe_load(f) if p.endswith((".yml", ".yaml")) else json.load(f)

S = load("context/scopes.json")
P = load("router/policy.json")
MAP = load("context/runner-map.json")
COMPOSE = load("compose.scopes.yml")

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: fails.append(name)

def chain(name):
    out, cur = [], name
    while cur: out.insert(0, cur); cur = S["nodes"][cur].get("parent")
    return out
def chainpath(name): return "workspace/scopes/" + "/".join(chain(name))

print("Ethical walls (C1):")
for a, b in S.get("conflicts", {}).get("pairs", []):
    ha, hb = MAP["scopeToHost"].get(a), MAP["scopeToHost"].get(b)
    check(f"conflict {a}/{b} on different runners", ha != hb and ha and hb, f"{ha} vs {hb}")

print("Isolation-trigger scopes get a dedicated runner:")
def is_root(n):
    ctl = {**S.get("levelDefaults", {}).get(S["nodes"][n]["level"], {}), **S["nodes"][n].get("controls", {})}
    members = {m for pr in S.get("conflicts", {}).get("pairs", []) for m in pr}
    return ctl.get("isolation") == "silo" or ctl.get("pii") == "block" or n in members
for n in S["nodes"]:
    if is_root(n):
        check(f"{n} not pooled to commons", MAP["scopeToHost"].get(n) != "claude-runner-commons")

print("Mount isolation — no runner can reach a foreign scope (C1 structural proof):")
roots = set(MAP["roots"])
for svc, cfg in COMPOSE["services"].items():
    scope_root = next((e.split("=", 1)[1] for e in cfg.get("environment", []) if e.startswith("SCOPE_ROOT=")), None)
    root_prefix = chainpath(scope_root) if scope_root and scope_root in S["nodes"] else None
    for vol in cfg.get("volumes", []):
        host = vol.split(":")[0]
        ro = vol.endswith(":ro")
        # never mount the whole tree
        check(f"{svc}: no wholesale workspace mount", host not in ("./workspace", "./workspace/scopes"), host)
        if not host.startswith("./workspace/scopes/"):
            continue  # claude-config etc.
        rel = host[len("./"):]
        if ro:
            # ancestor own-content only: path must be an ancestor's canonical slot
            anc_ok = any(rel.startswith(chainpath(a) + "/") and rel.split("/")[-1] in ("CLAUDE.md", "skills", "glossary.md")
                         for a in chain(scope_root)[:-1]) if scope_root else False
            check(f"{svc}: ro mount is an ancestor slot ({rel.split('/')[-1]})", anc_ok, rel)
        else:
            # rw must live inside this runner's own root subtree
            check(f"{svc}: rw mount within its own root", root_prefix and rel.startswith(root_prefix), rel)
            # and must NOT be inside a NON-ancestor root's subtree (ancestors are
            # legitimate path prefixes; siblings/cousins are the real violation)
            anc = set(chain(scope_root)) if scope_root else set()
            foreign = [r for r in roots if r not in anc and rel.startswith(chainpath(r) + "/")]
            check(f"{svc}: rw mount not inside a foreign root", not foreign, f"{rel} ⊂ {foreign}")

print("Operating-mode caps (Executive AI Compass):")
RANK = {"advise": 1, "read": 1, "write": 2, "ingest": 2, "apply": 3, "send": 3, "execute": 3}
mp = P["modes"]["map"]
check("human-only caps at advise", RANK[mp["human-only"]["maxAction"]] == 1)
check("human-led cannot apply/send", RANK[mp["human-led"]["maxAction"]] < 3)
check("ai-led allows execution", RANK[mp["ai-led"]["maxAction"]] == 3)
check("ai-led validates output", mp["ai-led"]["humanValidation"] == "output")
check("mode order most->least human is monotonic",
      [RANK[mp[m]["maxAction"]] for m in P["modes"]["order"]] == sorted(
          [RANK[mp[m]["maxAction"]] for m in P["modes"]["order"]], reverse=True))

print("Risk monotonicity (more access/autonomy never REMOVES a control):")
R = P["risk"]
def controls(data, access, autonomy):
    dims = {"data": data, "access": access, "autonomy": autonomy}
    cbd = R["controlsByDimension"]
    on = lambda spec: any(dims[d] >= t for d, t in spec.items() if not d.startswith("_"))
    return {k: on(cbd[k]) for k in ["requireVerify", "requireApproval", "requireIsolation", "blockCrossVendor"]}
base = controls(1, 1, 1)
for dim, i in [("access", 1), ("autonomy", 2)]:
    hi = controls(1, 3, 1) if dim == "access" else controls(1, 1, 3)
    check(f"raising {dim} only adds controls", all(hi[k] or not base[k] for k in base))
check("apply/proactive triggers approval", controls(1, 3, 3)["requireApproval"])
check("confidential data blocks cross-vendor", controls(3, 1, 1)["blockCrossVendor"])

print("Economics / AIOps (AIOPS.md):")
L = P.get("limits") or {}
check("spend ceilings present (a request count cannot express a budget)",
      any(k in L for k in ("maxNotionalSpendPerScope", "maxNotionalSpendGlobal")),
      "add maxNotionalSpendPerScope / maxNotionalSpendGlobal to policy.limits")
for k in ("maxNotionalSpendPerScope", "maxNotionalSpendGlobal"):
    if k in L and L[k] is not None:
        check(f"{k} is a positive number", isinstance(L[k], (int, float)) and L[k] > 0, repr(L[k]))
check("breaker window is set (ceilings are meaningless without one)",
      isinstance(L.get("windowMinutes"), (int, float)),
      repr(L.get("windowMinutes")))
# The notional/billed distinction is the thing most likely to be misread later.
try:
    aiops = open(os.path.join(ROOT, "docs", "design", "AIOPS.md"), encoding="utf-8").read()
except OSError:
    aiops = ""
check("AIOPS.md states notional cost is not money spent",
      "notional" in aiops.lower() and "subscription" in aiops.lower(),
      "AIOPS.md must keep the notional-vs-billed warning")

print("PII guard configured:")
check("risk.data has block/warn/off", all(k in R["data"] for k in ("block", "warn", "off")))

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}"); sys.exit(1)
print("All conformance checks passed.")
