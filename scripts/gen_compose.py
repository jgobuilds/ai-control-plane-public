#!/usr/bin/env python3
"""Generate per-isolation-root runner services from context/scopes.json.

Phase 1 of the C1/C2 structural-isolation fix (see ISOLATION-FIX-PLAN.md).
Emits:
  - compose.scopes.yml    one claude runner per isolation root + a commons runner,
                          each mounting ONLY its ancestor own-content (ro) + its own
                          subtree (rw). Nothing else is on the filesystem, so a
                          Bash-capable agent cannot read sibling scopes.
  - context/runner-map.json   scope -> runnerHost, consumed by the router (phase 3).

Pure stdlib (host has Python; not Node). Run whenever the scope tree changes:
    python scripts/gen_compose.py
This is INERT until phase 5 cutover — it writes files, changes no running service.
"""
import json, os, sys, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# WHICH tree, and where its outputs may go. A REAL scope tree lives in the
# private overlay, never in the engine: `context/scopes.json` is tracked,
# published, and rendered onto the live Pages site, so real scope names would
# publish a client list plus a sensitivity ranking. See scripts/scope_source.py.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_source                                        # noqa: E402

# `--engine` forces the SAMPLE tree and writes into the engine, which is
# otherwise unreachable once an overlay exists: resolve() always prefers the
# overlay, so on any machine that has one, the engine's committed sample
# artifacts can never be regenerated. They are tracked, published, and asserted
# by conformance — so without this they rot silently until CI fails on a change
# nobody could reproduce locally. That is the same drift class the mount check
# was built for, one level up.
SCOPE_SRC = ({"path": os.path.join(ROOT, "context", "scopes.json"), "source": "engine",
              "overlay": None, "out_dir": ROOT, "publishable": True}
             if "--engine" in sys.argv else scope_source.resolve(ROOT))
SCOPES_PATH = SCOPE_SRC["path"]
OUT_DIR = SCOPE_SRC["out_dir"]
WS = os.path.join(ROOT, "workspace")
SLOTS = ["CLAUDE.md", "skills", "glossary.md"]   # canonical inheritable content
DEFAULT_TRIGGERS = ["silo", "pii:block", "conflict"]

def load():
    with open(SCOPES_PATH, encoding="utf-8") as f:
        return json.load(f)

def effective_controls(s, name):
    n = s["nodes"][name]
    ctl = dict(s.get("levelDefaults", {}).get(n["level"], {}))
    ctl.update(n.get("controls", {}))
    return ctl

def chain(s, name):
    out, cur, guard = [], name, 0
    while cur:
        guard += 1
        if guard > 64: sys.exit("scope tree cycle at " + name)
        out.insert(0, cur)
        cur = s["nodes"][cur].get("parent")
    return out

def children(s, name):
    return [k for k, v in s["nodes"].items() if v.get("parent") == name]

# A RETIRED scope gets no runner. Deploying a container for a finished
# engagement is idle attack surface with no work to do — and because the scope
# is then absent from runner-map.json, the router's targetFor refuses it
# outright under RUNNER_MAP=on ("no runner-map entry ... not falling back").
#
# That is the useful half: retirement stops being a label in a document and
# becomes an enforced state. Routing work to a closed engagement fails closed
# instead of quietly succeeding against a live container.
RETIRED = "retired"

def is_active(s, name):
    return str(s["nodes"][name].get("status", "")).lower() != RETIRED

def is_root(s, name, triggers, conflict_members):
    ctl = effective_controls(s, name)
    if "silo" in triggers and ctl.get("isolation") == "silo": return True
    if "pii:block" in triggers and ctl.get("pii") == "block": return True
    if "conflict" in triggers and name in conflict_members: return True
    return False

def nearest_root(s, name, roots):
    for anc in reversed(chain(s, name)):          # self..root
        if anc in roots: return anc
    return None                                    # -> commons

def scope_path(s, name):
    """The WORKSPACE-RELATIVE PATH of a scope — what SCOPE_ROOT must hold.

    The runner resolves SCOPE_ROOT as a path under /workspace and then requires
    every cwd to sit inside it. The router sends
    `posix.join(workspaceRoot, *chain)`, so SCOPE_ROOT has to be the same form.

    This emitted the scope NAME instead ("delivery", "commons"), which resolved
    to /workspace/delivery — a path that does not exist and contains nothing. On
    cutover EVERY silo runner would have refused EVERY request, with
    "outside this runner's scope root", which reads as a security refusal rather
    than a config error. Caught by running safeCwd against a real cwd before
    deploying, not by deploying.
    """
    root = s.get("workspaceRoot", "scopes")
    return "/".join([root] + chain(s, name))


def common_path(s, names):
    """Deepest shared ancestor path of several scopes, for the commons runner."""
    if not names:
        return ""
    parts = [scope_path(s, n).split("/") for n in names]
    out = []
    for seg in zip(*parts):
        if len(set(seg)) != 1:
            break
        out.append(seg[0])
    return "/".join(out)

def host_for(s, root):
    n = s["nodes"].get(root, {})
    if n.get("runnerHost"): return n["runnerHost"]
    return "claude-runner-" + re.sub(r"[^a-z0-9-]", "-", root.lower())

def existing_slots(node_chain):
    base = os.path.join(WS, "scopes", *node_chain)
    return [sl for sl in SLOTS if os.path.exists(os.path.join(base, sl))]

def rel(node_chain, slot=None):
    p = "./workspace/scopes/" + "/".join(node_chain)
    return p + ("/" + slot if slot else "")

def cont(node_chain, slot=None):
    p = "/workspace/scopes/" + "/".join(node_chain)
    return p + ("/" + slot if slot else "")

def subtree_has_other_root(s, node, roots, exclude):
    for c in children(s, node):
        if c != exclude and c in roots: return True
        if subtree_has_other_root(s, c, roots, exclude): return True
    return False

def rw_mounts(s, root, roots):
    """Own subtree of `root`, split around any nested roots."""
    mounts = []
    def walk(node):
        if subtree_has_other_root(s, node, roots, exclude=root):
            for sl in existing_slots(chain(s, node)):
                mounts.append(f"{rel(chain(s, node), sl)}:{cont(chain(s, node), sl)}")
            for c in children(s, node):
                if c in roots: continue            # nested root — excluded
                walk(c)
        else:
            ch = chain(s, node)
            mounts.append(f"{rel(ch)}:{cont(ch)}")   # clean subtree, wholesale rw
    walk(root)
    return mounts

def ro_ancestor_mounts(s, root):
    mounts = []
    for anc in chain(s, root)[:-1]:                # strict ancestors
        for sl in existing_slots(chain(s, anc)):
            mounts.append(f"{rel(chain(s, anc), sl)}:{cont(chain(s, anc), sl)}:ro")
    return mounts

# Policy precedence, strict first. A runner takes the strictest of the scopes it
# serves, because one process reads one env var.
PII_RANK = {"block": 2, "warn": 1, "off": 0}

def strictest_pii(s, names):
    # An empty set returning "off" would fail OPEN — the most permissive value
    # from the least information. Unreachable today (a runner with no scopes is
    # not generated), which is exactly when a default like that survives review
    # and bites later.
    if not names:
        return "warn"
    best = "off"
    for n in names:
        v = effective_controls(s, n).get("pii", "warn")
        if PII_RANK.get(v, 1) > PII_RANK.get(best, 0):
            best = v
    return best

def service(name, scope_root, volumes, pii_policy="warn", provider="claude"):
    # gemini-runner/server.js carries safeCwd and enforcePii byte-for-byte
    # identical to claude-runner's, deliberately — so the ONLY thing that kept
    # gemini unisolated was compose wiring: one shared container with
    # `./workspace:/workspace` and neither variable set. The code was ready; the
    # topology was not. That is why this generalises rather than reimplements.
    gemini = provider == "gemini"
    env = [
        "RUNNER_TOKEN=${RUNNER_TOKEN:?set RUNNER_TOKEN in .env}",
        "FIREWALL=on",
    ]
    if gemini:
        env += [
            "PROVIDER=gemini",
            "GEMINI_API_KEY=${GEMINI_API_KEY:-}",
            "AGENT_BIN=${AGENT_BIN:-gemini}",
            "PROMPT_FLAG=${PROMPT_FLAG:--p}",
            "EXTRA_ARGS=${GEMINI_EXTRA_ARGS:---yolo --output-format json}",
            "READONLY_ARGS=${GEMINI_READONLY_ARGS:-}",
        ]
    else:
        env += [
            # AUTH. The base claude-runner carries this and the generated ones did
            # not, so the isolation cutover silently unauthenticated every claude
            # runner: the token was sitting in .env the whole time and simply was
            # not passed through. Every /route to a claude tier failed, and the
            # router reported 200 with an empty result (finding D1), so the
            # symptom was invisible for as long as both bugs held.
            #
            # A generated service must reach ENV PARITY with the hand-written one
            # it replaces. Omitting a variable here is not a smaller config, it is
            # a different one.
            "CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN:-}",
            "DEFAULT_MAX_TURNS=${DEFAULT_MAX_TURNS:-40}",
            "FANOUT_CONCURRENCY=${FANOUT_CONCURRENCY:-4}",
            # MCP wiring, same reason — `claude doctor` reports these as invalid
            # settings when absent, and the ask-human seam is how a run asks a
            # human mid-task rather than guessing.
            "N8N_API_URL=${N8N_API_URL:-}",
            "N8N_API_KEY=${N8N_API_KEY:-}",
            "ASK_HUMAN_URL=${ASK_HUMAN_URL:-http://n8n:5678/webhook/ask-human}",
            # Presented to that webhook; a distinct secret so n8n never holds
            # RUNNER_TOKEN (threat-model C3). Required, like the hand-written
            # runner: a generated service must reach env parity with it.
            "ASK_HUMAN_TOKEN=${ASK_HUMAN_TOKEN:?set a DISTINCT ASK_HUMAN_TOKEN in .env}",
        ]
    env += [
        f"SCOPE_ROOT={scope_root}",
        # The runner enforces this itself. Emitting SCOPE_ROOT without a
        # PII policy would repeat the exact defect this pass fixed: a
        # variable the compose file states and no code acts on.
        f"PII_POLICY={pii_policy}",
    ]
    return {
        "build": "./gemini-runner" if gemini else "./claude-runner",
        "container_name": name,
        "restart": "unless-stopped",
        "cap_add": ["NET_ADMIN"],
        "environment": env,
        # Home-level volume FIRST, then the tool dir nested on top. HOME is
        # /home/node, and the CLI writes `.claude.json` BESIDE `.claude/` — so a
        # volume covering only the tool dir loses config and session state on
        # every recreate. Proven by planting a marker in each location and
        # recreating: the one inside the volume survived, the one beside it did
        # not. The first fix landed only in docker-compose.yml, so the GENERATED
        # runners kept the old shape and the probe still vanished — the same
        # split that let CLAUDE_CODE_OAUTH_TOKEN go missing from these services.
        "volumes": ([f"{'gemini' if gemini else 'claude'}-home:/home/node",
                     ("gemini-config:/home/node/.gemini" if gemini
                      else "claude-config:/home/node/.claude")] + volumes),
        "networks": ["agentnet"],
        # Resource ceilings (threat-model M7), matching claude-runner in the base
        # compose file. These are the SILO runners — the containers that hold
        # tenant work — so leaving them unbounded while bounding the shared
        # runner would put the cap on the least sensitive one. That is precisely
        # what happened on the first pass: limits landed in docker-compose.yml,
        # the gate asserted them there, and five generated runners stayed
        # unbounded with the suite green.
        #
        # memswap_limit == mem_limit, else a container at its ceiling escapes
        # into host swap and the limit only slows the failure down.
        "mem_limit": "3g",
        "memswap_limit": "3g",
        "cpus": "2.0",
        "pids_limit": 512,
    }

def main():
    print(scope_source.describe(SCOPE_SRC))
    s = load()
    conflict_members = {m for pair in s.get("conflicts", {}).get("pairs", []) for m in pair}
    triggers = s.get("isolationTriggers", DEFAULT_TRIGGERS)
    roots = [n for n in s["nodes"]
             if is_active(s, n) and is_root(s, n, triggers, conflict_members)]
    retired = [n for n in s["nodes"] if not is_active(s, n)]

    # scope -> host
    scope_map, commons_nodes = {}, []
    for n in s["nodes"]:
        if not is_active(s, n):
            continue          # no host: the router must refuse, not route
        r = nearest_root(s, n, roots)
        if r is None:
            commons_nodes.append(n)
            scope_map[n] = "claude-runner-commons"
        else:
            scope_map[n] = host_for(s, r)

    # ethical-wall invariant
    for a, b in s.get("conflicts", {}).get("pairs", []):
        if scope_map.get(a) == scope_map.get(b):
            sys.exit(f"INVARIANT VIOLATION: conflict pair {a}/{b} share host {scope_map.get(a)}")

    services = {}
    for r in roots:
        # STRICTEST policy across every scope this runner serves, not the
        # root's own. A runner is shared by its whole subtree and reads one
        # env var, so it cannot be two policies at once — and the policy is
        # environmental precisely so a caller cannot choose it. Taking the
        # root's value would let a `block` descendant's work run on a `warn`
        # runner, which is the bypass this whole change exists to close.
        #
        # The cost is real and deliberate: a `warn` scope inside a `block`
        # root is enforced at `block`. That refuses some work the scope alone
        # would permit. Erring strict fails closed; erring loose leaks. A
        # scope that genuinely needs the looser rule needs its own root.
        _pii = strictest_pii(s, [n for n, h in scope_map.items()
                                 if h == host_for(s, r)])
        services[host_for(s, r)] = service(host_for(s, r), scope_path(s, r),
                                           ro_ancestor_mounts(s, r) + rw_mounts(s, r, roots),
                                           pii_policy=_pii)

    # commons: maximal clean subtrees whose parent is a root/None
    if commons_nodes:
        tops = [n for n in commons_nodes
                if (s["nodes"][n].get("parent") in roots) or (s["nodes"][n].get("parent") is None)]
        vols, seen = [], set()
        for t in tops:
            for anc in chain(s, t)[:-1]:
                for sl in existing_slots(chain(s, anc)):
                    m = f"{rel(chain(s, anc), sl)}:{cont(chain(s, anc), sl)}:ro"
                    if m not in seen: seen.add(m); vols.append(m)
            # SPLIT AROUND NESTED ROOTS, exactly as a root runner does. This
            # mounted the subtree WHOLESALE, so the pooled runner swallowed any
            # isolation root beneath it: commons could read `delivery`, a
            # pii:block scope with its own dedicated runner. Proven by writing a
            # file into delivery and cat-ing it from commons — C1, inside the
            # generator that exists to close C1.
            vols.extend(rw_mounts(s, t, roots))
        commons_root = common_path(s, commons_nodes)
        commons_pii = strictest_pii(s, commons_nodes)
        services["claude-runner-commons"] = service(
            "claude-runner-commons", commons_root, vols, pii_policy=commons_pii)
        # GEMINI IS POOL-ONLY, and that is a derivation rather than a
        # simplification. The triggers that create an isolation root — `silo`
        # and `pii:block` — are exactly the ones that put the risk model's data
        # dimension at 3, which is where `blockCrossVendor` fires. So policy can
        # never route a root's work to a second vendor, and a per-root gemini
        # container would be an idle container by construction. Checked against
        # both trees: every root in each scores blockCrossVendor.
        #
        # It gets the SAME mounts as the claude commons runner, so the pooled
        # set is the pooled set regardless of vendor. Anything a root owns is
        # absent from this filesystem, which is the C1 property, now on both
        # sides instead of one.
        services["gemini-runner-commons"] = service(
            "gemini-runner-commons", commons_root, list(vols),
            pii_policy=commons_pii, provider="gemini")

    out = {"services": services}
    header = ("# GENERATED by scripts/gen_compose.py from context/scopes.json — DO NOT EDIT.\n"
              "# One claude runner per isolation root; each mounts only its ancestor own-\n"
              "# content (ro) + its own subtree (rw). Use with the base file at cutover:\n"
              "#   docker compose -f docker-compose.yml -f compose.scopes.yml up\n")
    try:
        import yaml
        body = yaml.safe_dump(out, sort_keys=False, default_flow_style=False, width=120)
    except ImportError:
        sys.exit("PyYAML required (pip install pyyaml)")
    compose_out = os.path.join(OUT_DIR, "compose.scopes.yml")
    os.makedirs(os.path.dirname(compose_out) or ".", exist_ok=True)
    with open(compose_out, "w", encoding="utf-8") as f:
        f.write(header + body)
    map_out = os.path.join(OUT_DIR, "context", "runner-map.json")
    os.makedirs(os.path.dirname(map_out), exist_ok=True)
    with open(map_out, "w", encoding="utf-8") as f:
        # scopeToHost stays claude-only so every existing consumer keeps working.
        # providerHosts adds the second vendor WITHOUT a parallel scope map,
        # because two maps that must agree are two maps that will not: a scope
        # present in one and missing from the other is a routing bug nothing
        # would catch. A scope is gemini-reachable iff it is pooled, so the
        # router derives that from `roots`, which it already reads.
        json.dump({"generatedFrom": "scopes.json", "scopeToHost": scope_map,
                   "roots": roots, "hosts": sorted(set(scope_map.values())),
                   "providerHosts": {
                       "gemini": ("gemini-runner-commons" if commons_nodes else None)},
                   "_providerNote":
                       "gemini serves POOLED scopes only. Isolation roots score "
                       "blockCrossVendor (silo/pii:block => data 3), so policy already "
                       "refuses them a second vendor; the router escalates such a "
                       "request to the cheapest permitted claude tier rather than "
                       "refusing it."},
                  f, indent=2)
        f.write("\n")

    # topology report
    print("Isolation roots:", ", ".join(roots) or "(none)")
    print("Commons nodes  :", ", ".join(commons_nodes) or "(none)")
    print("Runner services:", len(services))
    if retired:
        # Say what was skipped. A silently smaller topology reads as a bug.
        print("Retired, no runner:", ", ".join(sorted(retired)))
    for host in sorted(services):
        # The second-vendor runner is not in scope_map (that map is claude-only,
        # deliberately — see the runner-map comment). Reporting it as serving
        # nothing would read as a broken service rather than a pooled one.
        served = (commons_nodes if host.startswith("gemini-")
                  else [n for n, h in scope_map.items() if h == host])
        note = "  [second vendor; pooled scopes only]" if host.startswith("gemini-") else ""
        print(f"  {host:28s} serves: {', '.join(served) or '(none)'}{note}")
    missing = [host_for(s, r) for r in roots
               if not os.path.isdir(os.path.join(WS, "scopes", *chain(s, r)))]
    if missing:
        print("\nNOTE: these root scope dirs don't exist yet (create before `up`):")
        for r in roots:
            d = os.path.join("workspace", "scopes", *chain(s, r))
            if not os.path.isdir(os.path.join(ROOT, d)): print(f"  {d}")

if __name__ == "__main__":
    main()
