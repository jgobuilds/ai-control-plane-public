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
SCOPES_PATH = os.path.join(ROOT, "context", "scopes.json")
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

def service(name, scope_root, volumes):
    return {
        "build": "./claude-runner",
        "container_name": name,
        "restart": "unless-stopped",
        "cap_add": ["NET_ADMIN"],
        "environment": [
            "RUNNER_TOKEN=${RUNNER_TOKEN:?set RUNNER_TOKEN in .env}",
            "FIREWALL=on",
            "DEFAULT_MAX_TURNS=${DEFAULT_MAX_TURNS:-40}",
            "FANOUT_CONCURRENCY=${FANOUT_CONCURRENCY:-4}",
            f"SCOPE_ROOT={scope_root}",
        ],
        "volumes": ["claude-config:/home/node/.claude"] + volumes,
        "networks": ["agentnet"],
    }

def main():
    s = load()
    conflict_members = {m for pair in s.get("conflicts", {}).get("pairs", []) for m in pair}
    triggers = s.get("isolationTriggers", DEFAULT_TRIGGERS)
    roots = [n for n in s["nodes"] if is_root(s, n, triggers, conflict_members)]

    # scope -> host
    scope_map, commons_nodes = {}, []
    for n in s["nodes"]:
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
        services[host_for(s, r)] = service(host_for(s, r), r,
                                           ro_ancestor_mounts(s, r) + rw_mounts(s, r, roots))

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
            ch = chain(s, t)
            vols.append(f"{rel(ch)}:{cont(ch)}")
        services["claude-runner-commons"] = service("claude-runner-commons", "commons", vols)

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
    with open(os.path.join(ROOT, "compose.scopes.yml"), "w", encoding="utf-8") as f:
        f.write(header + body)
    with open(os.path.join(ROOT, "context", "runner-map.json"), "w", encoding="utf-8") as f:
        json.dump({"generatedFrom": "scopes.json", "scopeToHost": scope_map,
                   "roots": roots, "hosts": sorted(set(scope_map.values()))}, f, indent=2)
        f.write("\n")

    # topology report
    print("Isolation roots:", ", ".join(roots) or "(none)")
    print("Commons nodes  :", ", ".join(commons_nodes) or "(none)")
    print("Runner services:", len(services))
    for host in sorted(services):
        served = [n for n, h in scope_map.items() if h == host]
        print(f"  {host:28s} serves: {', '.join(served)}")
    missing = [host_for(s, r) for r in roots
               if not os.path.isdir(os.path.join(WS, "scopes", *chain(s, r)))]
    if missing:
        print("\nNOTE: these root scope dirs don't exist yet (create before `up`):")
        for r in roots:
            d = os.path.join("workspace", "scopes", *chain(s, r))
            if not os.path.isdir(os.path.join(ROOT, d)): print(f"  {d}")

if __name__ == "__main__":
    main()
