#!/usr/bin/env python3
"""Prove scripts/mount_drift_check.py actually fires.

WHY THIS EXISTS. The drift check reports on slot files that exist on disk. A
tree with no `CLAUDE.md` anywhere gives it nothing to look at, and it prints
"PASS 0 (scope-slot, runner) pair(s)" — green, and checking nothing. That is
exactly how the audit-ledger gate went vacuous, and the real scope tree has no
slot files today, so this is not hypothetical.

So the test synthesises the failure instead of hoping the tree provides one: it
writes a slot file the generated compose cannot know about, asserts the check
FAILS, and removes it. If someone weakens the check, this goes red whatever is
on disk.

    python tests/mount_drift_test.py

It touches the ACTIVE workspace briefly (creating a file and deleting it in a
finally). Pointing it at a fixture tree would be cleaner, but the check resolves
its paths at import from the repo root, and a fixture would test a copy of the
logic rather than the thing CI runs.
"""
import json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import scope_source                                        # noqa: E402

CHECK = os.path.join(ROOT, "scripts", "mount_drift_check.py")
SRC = scope_source.resolve(ROOT)


def run():
    r = subprocess.run([sys.executable, CHECK], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def chain(nodes, name):
    out, cur = [], name
    while cur:
        out.insert(0, cur)
        cur = nodes[cur].get("parent")
    return out


def main():
    with open(SRC["path"], encoding="utf-8") as f:
        S = json.load(f)
    map_path = os.path.join(SRC["out_dir"], "context", "runner-map.json")
    with open(map_path, encoding="utf-8") as f:
        MAP = json.load(f)

    # Baseline first. If the tree is already drifting, a red result after our
    # edit would prove nothing — the test would pass on someone else's failure.
    code, out = run()
    if code != 0:
        print("SKIP — the active tree already has drift; fix that first:\n" + out)
        return 0

    # A scope with a runner, whose glossary.md does not already exist.
    target = None
    for name in MAP.get("scopeToHost", {}):
        if name not in S["nodes"]:
            continue
        rel = os.path.join("workspace", "scopes", *chain(S["nodes"], name), "glossary.md")
        if not os.path.exists(os.path.join(ROOT, rel)) and \
           os.path.isdir(os.path.dirname(os.path.join(ROOT, rel))):
            target, target_rel = name, rel
            break
    if target is None:
        print("SKIP — no active scope without a glossary.md to use as the probe")
        return 0

    path = os.path.join(ROOT, target_rel)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("# drift probe — created and removed by tests/mount_drift_test.py\n")
        code, out = run()
        if code == 0:
            print(f"FAIL — added {target_rel} after generation and the check stayed green.\n{out}")
            return 1
        if "missing-mount" not in out:
            print(f"FAIL — the check failed, but not with missing-mount:\n{out}")
            return 1
        print(f"PASS  drift check catches a slot file added after generation ({target}/glossary.md)")
    finally:
        if os.path.exists(path):
            os.remove(path)

    # And it must go green again once the probe is gone — a check that fails
    # permanently after one bad run is not a gate, it is an alarm nobody silences.
    code, out = run()
    if code != 0:
        print(f"FAIL — still red after removing the probe:\n{out}")
        return 1
    print("PASS  green again once the probe is removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
