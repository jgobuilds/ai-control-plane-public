#!/usr/bin/env python3
"""Prove .github/dependabot.yml points at directories that actually exist.

WHY THIS EXISTS — it is the harness fix for a recurrence, not a hypothetical.
The docker ecosystem was configured with `directory: "/"`, and the updater
aborted every single day with:

    ERROR <job_...> Error during file fetching; aborting:
    No Dockerfiles nor Kubernetes YAML found in /

Five identical red runs before anyone escalated, because each individual morning
it was just one red run — which is the whole argument for the recurrence
register. The damage was not the red X. Dependency-security has two halves: a
scanner that TELLS you a dependency is vulnerable, and Dependabot that OPENS THE
PR that fixes it. The scanner half kept reporting, so the repo looked covered
while the fixing half had been dead for five days.

A misconfigured path is the failure mode here, and it is entirely checkable
without network, credentials, or GitHub: an ecosystem declares a directory, and
that directory either holds the manifest that ecosystem updates, or it does not.

    python scripts/dependabot_check.py

Exits non-zero with the specific offending entry. Stdlib + pyyaml.
"""
from __future__ import annotations
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, ".github", "dependabot.yml")

# What each ecosystem needs to find in its declared directory. Only ecosystems
# this repo uses — guessing at the other twenty would be inventing rules nobody
# has tested. An ecosystem not listed here is reported as unchecked, not passed.
# Every entry must declare `cooldown: { default-days: N }` with N >= this. It is
# the minimum release age the pins were chosen by (docs/runbooks/UPGRADES.md):
# a new release is not proposed until it has been public this long. Security
# updates are not subject to cooldown (GitHub options reference), so this never
# delays a fix. Checked because the failure is silent — an entry without it falls
# back to Dependabot's 3-day default and nothing says so.
MIN_COOLDOWN_DAYS = 7

MANIFESTS = {
    "npm": ("package.json",),
    "docker": ("Dockerfile",),
    "pip": ("requirements.txt", "pyproject.toml", "setup.py"),
    "github-actions": (".github/workflows",),
}


def main():
    if not os.path.isfile(CONFIG):
        print(f"no {os.path.relpath(CONFIG, ROOT)} — nothing to check")
        return 0
    try:
        import yaml
    except ImportError:
        print("FAIL pyyaml is not installed, so this check cannot run — "
              "and a check that cannot run is not a pass")
        return 2

    with open(CONFIG, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    updates = cfg.get("updates") or []
    if not updates:
        print("FAIL dependabot.yml declares no updates — the fixing half of "
              "dependency-security is off")
        return 1

    bad, unchecked, ok = [], [], 0
    cooldown_bad = []
    for u in updates:
        eco = str(u.get("package-ecosystem") or "?")
        where = f"{eco} -> {u.get('directory') or u.get('directories')}"
        cd = u.get("cooldown")
        days = cd.get("default-days") if isinstance(cd, dict) else None
        if not isinstance(days, int) or isinstance(days, bool):
            cooldown_bad.append(f"{where}: no cooldown.default-days (Dependabot silently "
                                f"uses its 3-day default)")
        elif days < MIN_COOLDOWN_DAYS:
            cooldown_bad.append(f"{where}: cooldown {days}d is below the "
                                f"{MIN_COOLDOWN_DAYS}-day minimum release age")
        # `directories` (plural, glob list) is also valid; handle both.
        dirs = u.get("directories") or [u.get("directory") or "/"]
        wanted = MANIFESTS.get(eco)
        if not wanted:
            unchecked.append(eco)
            continue
        for d in dirs:
            if "*" in str(d):
                unchecked.append(f"{eco} {d} (glob)")
                continue
            base = os.path.join(ROOT, str(d).lstrip("/").replace("/", os.sep))
            if any(os.path.exists(os.path.join(base, w.replace("/", os.sep)))
                   for w in wanted):
                ok += 1
            else:
                bad.append(f"{eco} -> {d}: no {' or '.join(wanted)} there")

    for b in bad:
        print("  BAD  " + b)
    for c in cooldown_bad:
        print("  BAD  " + c)
    for u in sorted(set(unchecked)):
        print("  --   unchecked ecosystem: " + u)
    if bad:
        print(f"\nFAIL {len(bad)} dependabot entry(ies) point at a directory with no "
              "manifest.\n     The updater aborts on these, so no bump PRs are opened "
              "at all — the\n     scanner keeps reporting and the repo looks covered "
              "while it is not.")
        return 1
    if cooldown_bad:
        print(f"\nFAIL {len(cooldown_bad)} dependabot entry(ies) without a >= "
              f"{MIN_COOLDOWN_DAYS}-day cooldown.\n     Version updates would be proposed "
              "on a newer release than the pin policy\n     allows. (Security updates are "
              "unaffected either way.)")
        return 1
    print(f"PASS dependabot config: {ok} entry(ies) resolve to a real manifest, "
          f"all with a >= {MIN_COOLDOWN_DAYS}-day cooldown"
          + (f", {len(set(unchecked))} unchecked" if unchecked else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
