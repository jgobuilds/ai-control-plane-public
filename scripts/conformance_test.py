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

# Resolve the ACTIVE tree, exactly as gen_compose and mount_drift_check do. This
# used to load the engine's paths unconditionally, which meant the topology
# actually deployed on a workstation — the real tree, its generated compose, its
# runner map — was never conformance-checked at all. Every C1 assertion below ran
# against the sample and nothing else. In CI there is no overlay, so this still
# checks the sample there; the difference is that locally it now checks what is
# running.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_source                                        # noqa: E402
SRC = scope_source.resolve(ROOT)
_out = SRC["out_dir"]

def load_abs(p):
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) if p.endswith((".yml", ".yaml")) else json.load(f)

S = load_abs(SRC["path"])
P = load("router/policy.json")            # engine config, not tree-derived
MAP = load_abs(os.path.join(_out, "context", "runner-map.json"))
COMPOSE = load_abs(os.path.join(_out, "compose.scopes.yml"))
print(f"Active tree: {SRC['source']}  ({SRC['path']})\n")

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond: fails.append(name)

class TopologyError(Exception):
    """scopes.json is malformed in a way that makes the isolation proof impossible."""


def chain(name):
    """Root-to-leaf ancestry for a scope name.

    Raises TopologyError instead of KeyError, and refuses to loop. The naive
    version walked `parent` pointers with no guards, which gave three ways to
    fail badly rather than clearly — all three reachable, all three verified:

      unknown name      KeyError, which is how issue #7 happened: the gate CRASHED
                        mid-run instead of failing, so ci_diagnose saw a traceback,
                        matched no signature, and filed an "unrecognised failure".
      dangling parent   KeyError again, from deleting a scope whose children still
                        name it.
      parent cycle      NOT an exception — an unbounded loop. The job hangs to the
                        runner timeout while `out` grows, which is the worst of the
                        three: no error text to diagnose and six hours of runner
                        time burned.

    A malformed tree is a real finding, not an accident to swallow: if ancestry
    cannot be resolved, the C1 mount proof below cannot be computed at all.
    """
    out, cur, seen = [], name, set()
    while cur:
        if cur not in S["nodes"]:
            raise TopologyError(
                f"scope {cur!r} is named in the tree but is not a node in "
                f"{SRC['path']}" + (f" (walking ancestry of {name!r})" if cur != name else ""))
        if cur in seen:
            raise TopologyError(
                f"parent cycle in scopes.json: {' -> '.join(out)} -> {cur} "
                f"(reached while walking ancestry of {name!r})")
        seen.add(cur)
        out.insert(0, cur)
        cur = S["nodes"][cur].get("parent")
    return out
def chainpath(name): return "workspace/scopes/" + "/".join(chain(name))

# PRECONDITION, checked before anything that depends on it. The C1 mount proof is
# expressed entirely in terms of ancestry, so a malformed tree does not weaken it
# — it makes it uncomputable. Running it anyway is how a crash gets mistaken for a
# verdict, so this stops the gate HERE and says which checks did not run. An
# unrun check is `unknown`, never `pass`.
print("Scope topology resolves (precondition for the C1 proof):")
_bad = []
for _n in S["nodes"]:
    try:
        chain(_n)
    except TopologyError as e:
        _bad.append((_n, str(e)))
check("every scope's ancestry resolves to a root", not _bad,
      "; ".join(f"{n}: {m}" for n, m in _bad[:3]))
if _bad:
    print()
    print(f"FAILED: malformed scope topology in {SRC['path']} — {len(_bad)} scope(s) "
          f"cannot be resolved.")
    print("  The mount-isolation proof (C1) was NOT evaluated: it is defined in terms")
    print("  of ancestry, so it cannot run against a tree whose ancestry is broken.")
    print("  Those checks are UNKNOWN, not passing. Fix the tree and re-run.")
    sys.exit(1)

print("The runner map points at services that exist:")
# ENOTFOUND, six times over two days of the per-isolation-root migration: the map
# named a hostname no service answered to, and nothing compared the two, so the
# first thing to notice was a request failing in flight. Deliberately UNCONDITIONAL
# — the gemini assertions below only run when the tree has pooled scopes, which
# means they are absent in CI, where the sample tree has none. A proof that does
# not run where the gate runs is not a proof.
_hosts = set((MAP.get("scopeToHost") or {}).values())
_hosts |= {h for h in (MAP.get("providerHosts") or {}).values() if h}
_missing = sorted(h for h in _hosts if h not in COMPOSE.get("services", {}))
check("every runner-map host is a service the compose defines", not _missing,
      f"{_missing} named in the map, absent from compose")

print("Ethical walls (C1):")
for a, b in S.get("conflicts", {}).get("pairs", []):
    ha, hb = MAP["scopeToHost"].get(a), MAP["scopeToHost"].get(b)
    check(f"conflict {a}/{b} on different runners", ha != hb and ha and hb, f"{ha} vs {hb}")

print("Second-vendor runners are pool-only and scoped:")
# gemini used to be one shared container with `./workspace:/workspace` and no
# SCOPE_ROOT — the C1/C2 residual. These assert the replacement holds, and the
# pool-only rule specifically: an isolation root must never get a gemini runner,
# because `silo`/`pii:block` put data at 3 where blockCrossVendor fires, so such
# a container could only ever sit idle while holding the mounts.
def env_of(svc):
    return dict(e.split("=", 1) for e in COMPOSE["services"][svc].get("environment", [])
                if "=" in e)

gem = sorted(n for n in COMPOSE["services"] if n.startswith("gemini-"))
pooled = "claude-runner-commons" in MAP["scopeToHost"].values()
ph = (MAP.get("providerHosts") or {}).get("gemini")

# A second-vendor runner exists IFF there are pooled scopes to serve. The sample
# tree has none — every scope sits under an isolation root — so asserting one
# always exists would fail there for the right reason and the wrong outcome.
if pooled:
    check("pooled scopes get a second-vendor runner", bool(gem), "gen_compose emitted none")
    check("providerHosts.gemini names that runner", ph in COMPOSE["services"], repr(ph))
else:
    check("no pooled scopes => no second-vendor runner", not gem, f"{gem} serve nothing")
    check("no pooled scopes => providerHosts.gemini is null", ph is None, repr(ph))

for svc in gem:
    envs = env_of(svc)
    check(f"{svc}: SCOPE_ROOT is set", bool(envs.get("SCOPE_ROOT")),
          "an UNSCOPED second-vendor runner is the original C1/C2 residual")
    check(f"{svc}: PII_POLICY is set", bool(envs.get("PII_POLICY")))
    # The strongest available invariant, and the cheapest to reason about: the
    # second-vendor runner serves EXACTLY the pooled set, so its scope root and
    # PII policy must equal the claude commons runner's. If they ever diverge,
    # one vendor is reaching something the other cannot.
    if "claude-runner-commons" in COMPOSE["services"]:
        cc = env_of("claude-runner-commons")
        check(f"{svc}: same SCOPE_ROOT as claude commons",
              envs.get("SCOPE_ROOT") == cc.get("SCOPE_ROOT"),
              f"{envs.get('SCOPE_ROOT')} vs {cc.get('SCOPE_ROOT')}")
        check(f"{svc}: same PII_POLICY as claude commons",
              envs.get("PII_POLICY") == cc.get("PII_POLICY"),
              f"{envs.get('PII_POLICY')} vs {cc.get('PII_POLICY')}")

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
    # SCOPE_ROOT holds a workspace-relative PATH ("scopes/a/b"), not a scope
    # NAME. It used to hold the name, and this file both looked it up in
    # S["nodes"] and passed it to chain() — so it raised KeyError the moment the
    # generator began emitting the correct value.
    #
    # Two derived forms are needed, and conflating them is what broke:
    #   root_prefix  compares against `rel`, which carries the workspace/ prefix
    #   scope_name   the last segment, the only thing chain() can resolve
    root_prefix = ("workspace/" + scope_root.strip("/")) if scope_root else None
    scope_name = scope_root.strip("/").split("/")[-1] if scope_root else None
    if scope_name not in S["nodes"]:
        # A commons runner's root can be a shared ANCESTOR path whose last
        # segment is not itself a scope. Guard rather than KeyError.
        scope_name = None
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
                         for a in chain(scope_name)[:-1]) if scope_name else False
            check(f"{svc}: ro mount is an ancestor slot ({rel.split('/')[-1]})", anc_ok, rel)
        else:
            # rw must live inside this runner's own root subtree
            check(f"{svc}: rw mount within its own root", root_prefix and rel.startswith(root_prefix), rel)
            # and must NOT be inside a NON-ancestor root's subtree (ancestors are
            # legitimate path prefixes; siblings/cousins are the real violation)
            anc = set(chain(scope_name)) if scope_name else set()
            foreign = [r for r in roots if r not in anc and rel.startswith(chainpath(r) + "/")]
            check(f"{svc}: rw mount not inside a foreign root", not foreign, f"{rel} ⊂ {foreign}")
            # ...and must not CONTAIN one either. The check above is one-
            # directional: it caught a mount sitting inside someone else's root
            # and missed a mount that SWALLOWS one. The commons runner mounted a
            # whole business subtree that contained `delivery`, a pii:block root
            # with its own runner — so the pooled runner could read the isolated
            # scope's files. Verified by writing a file into delivery and
            # `cat`-ing it from commons. That is C1, in the code that exists to
            # close C1.
            swallowed = [r for r in roots
                         if chainpath(r) != rel and chainpath(r).startswith(rel.rstrip("/") + "/")]
            check(f"{svc}: rw mount does not contain a foreign root",
                  not swallowed, f"{rel} ⊃ {swallowed}")

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

print("Action binding — the router decides what a request may DO (router/actions.js):")
# The enforce switch fails CLOSED: only the literal "warn" relaxes it, so a
# missing key means "block", and "block" 400s every /route call that omits a
# verb — which is most of them. That is correct for a typo and a disaster for an
# omission, so the choice has to be written down. This check is the second guard;
# tests/security_test.py is the first.
_E = R.get("enforce") or {}
check('policy.json states risk.enforce.actionBinding explicitly ("warn" or "block")',
      _E.get("actionBinding") in ("warn", "block"),
      f"got {_E.get('actionBinding')!r} — a missing key enforces, so an omission would block live traffic")

# Every declared operation must be something the router can bind. Mirrors
# operationProblems() in router/actions.js, which is canonical — kept to the
# structural rules so the two cannot drift on meaning: the verb exists, the
# irreversible flag is a boolean, and every tool is a name the runner can
# actually enforce (a bare built-in or a whole MCP server — never a specifier,
# never one tool of a server).
_VERBS = {k: v for k, v in (R.get("access") or {}).items() if k not in ("default", "_comment")}
_TA = R.get("toolAccess") or {}
_TOOLS = _TA.get("tools") or {}
def _mcp_server(t):
    import re as _re
    m = _re.match(r"^mcp__([^_](?:[^_]|_(?!_))*)(?:__|$)", t)
    return f"mcp__{m.group(1)}" if m else None
def _tool_verb(t):
    for k in (t, t.split("(", 1)[0], _mcp_server(t)):
        if k and isinstance(_TOOLS.get(k), str):
            return _TOOLS[k]
    return _TA.get("unlisted") if _TA.get("unlisted") in _VERBS else max(_VERBS, key=_VERBS.get)
_ops_seen = 0
for _n, _node in S["nodes"].items():
    _ctl = {**(S.get("levelDefaults", {}).get(_node.get("level"), {}) or {}), **(_node.get("controls") or {})}
    _ops = _ctl.get("operations")
    if _ops is None:
        continue
    check(f"{_n}: controls.operations is an object", isinstance(_ops, dict), repr(type(_ops)))
    if not isinstance(_ops, dict):
        continue
    _scope_tools = [t for t in (_ctl.get("allowedTools") or []) if isinstance(t, str) and t.strip()]
    for _op, _d in _ops.items():
        _ops_seen += 1
        _d = _d if isinstance(_d, dict) else {}
        check(f"{_n}/{_op}: access is a policy verb", _d.get("access") in _VERBS, repr(_d.get("access")))
        check(f"{_n}/{_op}: irreversible is a boolean when given",
              "irreversible" not in _d or isinstance(_d["irreversible"], bool), repr(_d.get("irreversible")))
        _tools = _d.get("tools")
        if _tools is None:
            continue
        check(f"{_n}/{_op}: tools is a list of names",
              isinstance(_tools, list) and all(isinstance(t, str) and t for t in _tools), repr(_tools))
        if not isinstance(_tools, list):
            continue
        _bad = [t for t in _tools if "(" in t or (_mcp_server(t) and _mcp_server(t) != t)]
        check(f"{_n}/{_op}: every tool is enforceable (bare built-in or whole MCP server)", not _bad,
              f"{_bad} — specifiers narrow nothing under skip-permissions; MCP is enforceable per server only")
        if _d.get("access") in _VERBS and _TOOLS:
            _over = [t for t in _tools if _VERBS.get(_tool_verb(t), 99) > _VERBS[_d["access"]]]
            check(f"{_n}/{_op}: no tool exceeds the operation's declared access", not _over,
                  f"{_over} can do more than {_d['access']!r} — the declaration understates the operation")
        if _scope_tools:
            # An operation that names a tool its own scope does not allow is
            # silently narrowed at dispatch — the agent arrives without the tool
            # the operation was declared around. Better to find out here.
            _missing = [t for t in _tools if t not in _scope_tools and t.split("(", 1)[0] not in _scope_tools]
            check(f"{_n}/{_op}: every tool is in the scope's allowedTools", not _missing,
                  f"{_missing} would be dropped at dispatch")
print(f"  (validated {_ops_seen} declared operation(s))")

print("PII guard configured:")
check("risk.data has block/warn/off", all(k in R["data"] for k in ("block", "warn", "off")))

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}"); sys.exit(1)
print("All conformance checks passed.")
