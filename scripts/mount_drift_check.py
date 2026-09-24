#!/usr/bin/env python3
"""Catch context files that exist on disk but are invisible to the runner.

THE FAILURE THIS EXISTS FOR. `gen_compose.py` computes mounts from what exists
on disk AT GENERATE TIME. Adding `workspace/scopes/<scope>/CLAUDE.md` after that
produces no mount, no warning, and no error — the runner simply never sees it.
The agent behaves as if the file were never written, and the only symptom is
worse output. Nothing else in the suite looks at this: conformance_test.py
checks that mounts are not TOO BROAD (the C1 direction) and never that they are
too narrow.

THREE CHECKS, and the third is the one that can't be faked:

  MISSING   a slot file exists on disk under an active scope, but the runner(s)
            that must see it have no volume covering it. Means: regenerate.

  PHANTOM   a compose volume points at a host path that does not exist. Docker
            CREATES the directory rather than failing, so the runner gets a
            silent empty dir where content was expected — the same invisible
            outcome, arrived at from the other direction.

  LIVE      the file is not readable INSIDE the running container. Catches what
            the static checks structurally cannot: correct file, correct compose,
            container never recreated. "Generated" and "deployed" are different
            claims and only this one tests the second.

Static checks read the ACTIVE tree (overlay when present — see scope_source.py),
so on a workstation this checks the real deployment and in CI it checks the
sample. Live mode needs docker and is skipped without it.

    python scripts/mount_drift_check.py            # static only (CI)
    python scripts/mount_drift_check.py --live     # + exec into each container
"""
import json, os, subprocess, sys
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_source                                        # noqa: E402

SRC = scope_source.resolve(ROOT)
WS = os.path.join(ROOT, "workspace")
SLOTS = ["CLAUDE.md", "skills", "glossary.md"]   # must match gen_compose.SLOTS

fails, warns = [], []


def fail(kind, msg):
    print(f"  FAIL  {kind}  {msg}")
    fails.append(kind)


def warn(kind, msg):
    print(f"  warn  {kind}  {msg}")
    warns.append(kind)


def load(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) if path.endswith((".yml", ".yaml")) else json.load(f)


S = load(SRC["path"])
COMPOSE_PATH = os.path.join(SRC["out_dir"], "compose.scopes.yml")
MAP_PATH = os.path.join(SRC["out_dir"], "context", "runner-map.json")
if not (os.path.isfile(COMPOSE_PATH) and os.path.isfile(MAP_PATH)):
    sys.exit(f"missing generated artifacts under {SRC['out_dir']} — run scripts/gen_compose.py")
COMPOSE, MAP = load(COMPOSE_PATH), load(MAP_PATH)

print(f"Active tree: {SRC['source']}  ({SRC['path']})")
print(f"Generated:   {COMPOSE_PATH}")


def chain(name):
    out, cur = [], name
    while cur:
        out.insert(0, cur)
        cur = S["nodes"][cur].get("parent")
    return out


def is_active(name):
    return (S["nodes"][name].get("status") or "active") != "retired"


def host_paths(svc):
    """Volume host paths for a service, normalised to repo-relative."""
    out = []
    for vol in COMPOSE["services"][svc].get("volumes", []):
        h = vol.split(":")[0]
        if h.startswith("./"):
            out.append(h[2:])
    return out


def covers(svc, rel):
    """Does any volume of `svc` put `rel` inside the container?

    Either the mount IS the path, or it is a directory ancestor of it. A mount
    of a sibling file never covers it, which is the whole point.
    """
    for h in host_paths(svc):
        if h == rel or rel.startswith(h.rstrip("/") + "/"):
            return True
    return False


# Runner roots: the scopes that own a container. Derived from the map rather
# than recomputed, so a bug in the trigger logic shows up as drift here instead
# of being reproduced identically by both sides.
roots = [r for r in MAP.get("roots", []) if r in S["nodes"]]

# ---------- MISSING: content on disk that no serving runner can see ----------
print("\nMISSING — slot exists on disk but its runner has no mount for it:")
checked = 0
for name in S["nodes"]:
    if not is_active(name):
        continue
    ch = chain(name)
    for slot in SLOTS:
        rel = "workspace/scopes/" + "/".join(ch) + "/" + slot
        if not os.path.exists(os.path.join(ROOT, rel.replace("/", os.sep))):
            continue
        # Who must see this file: the runner serving this scope, plus every
        # runner rooted at a DESCENDANT (they inherit ancestor own-content).
        required = set()
        if MAP.get("scopeToHost", {}).get(name):
            required.add(MAP["scopeToHost"][name])
        for r in roots:
            if name in chain(r)[:-1]:
                required.add(MAP["scopeToHost"].get(r))
        for svc in sorted(x for x in required if x and x in COMPOSE["services"]):
            checked += 1
            if not covers(svc, rel):
                fail("missing-mount", f"{svc} cannot see {rel} — regenerate with gen_compose.py")
if not fails:
    print(f"  PASS  {checked} (scope-slot, runner) pair(s) mounted")

# ---------- PHANTOM: mount whose host path is not there ----------
print("\nPHANTOM — compose mounts a host path that does not exist (docker would "
      "create an empty dir):")
before, before_warns = len(fails), len(warns)
for svc in COMPOSE["services"]:
    for h in host_paths(svc):
        if not h.startswith("workspace/scopes/"):
            continue
        if not os.path.exists(os.path.join(ROOT, h.replace("/", os.sep))):
            # Only an error for the tree actually in service. The engine's
            # sample workspace is deliberately partial — it illustrates a shape,
            # nothing runs against it — so flagging it would train people to
            # ignore this gate, which is worse than not having it.
            (warn if SRC["publishable"] else fail)(
                "phantom-mount", f"{svc}: {h} is mounted but absent on disk")
if len(fails) == before and len(warns) == before_warns:
    print("  PASS  every mounted host path exists")

# ---------- LIVE: is it actually inside the running container ----------
if "--live" in sys.argv:
    print("\nLIVE — reading the expected paths inside the running containers:")
    try:
        running = set(subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                                     capture_output=True, text=True, check=True,
                                     encoding="utf-8").stdout.split())
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"  SKIP  docker unavailable ({e})")
        running = None
    if running is not None:
        n = 0
        for svc, cfg in COMPOSE["services"].items():
            cname = cfg.get("container_name", svc)
            if cname not in running:
                print(f"  SKIP  {cname} not running")
                continue
            for vol in cfg.get("volumes", []):
                parts = vol.split(":")
                if not parts[0].startswith("./workspace/scopes/"):
                    continue
                cpath = parts[1]
                n += 1
                r = subprocess.run(["docker", "exec", cname, "test", "-e", cpath])
                if r.returncode != 0:
                    fail("not-deployed",
                         f"{cname}: {cpath} is in the generated compose but absent in the "
                         "RUNNING container — regenerated and never recreated")
        print(f"  checked {n} mounted path(s) live")

print()
if warns and not fails:
    print(f"{len(warns)} warning(s) on the sample tree; no failures.")
if fails:
    print(f"FAILED: {len(fails)} — {', '.join(sorted(set(fails)))}")
    sys.exit(1)
print("No mount drift.")
