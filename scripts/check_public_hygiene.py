#!/usr/bin/env python3
"""Public-hygiene guard — keep a private org identity out of this engine.

The engine (AI Control Plane) ships brand-neutral so any org can run it
with its own private overlay. This guard fails a commit if a staged engine file
carries a forbidden brand token or is an instance-only path.

What counts as "forbidden" is CONFIGURABLE, so it isn't tied to one org. Token
list resolution order:
  1. <overlay>/hygiene.json         (active *-instance overlay, or $AICP_INSTANCE)
  2. context/hygiene.json           (gitignored local override)
  3. context/hygiene.example.json   (committed neutral default: no tokens)

TWO MODES — and CI needs the second one:

    check_public_hygiene.py           # staged files — the commit-time gate
    check_public_hygiene.py --tree    # every TRACKED file — the CI gate

The distinction is load-bearing. Staged-only scans nothing in CI, where nothing is
ever staged after a checkout: it loops over an empty list and returns 0, green
while checking nothing. A gate that cannot fail is not a gate.

HONEST LIMIT OF THE CI RUN: the forbidden-token list lives in the PRIVATE overlay,
which by design is not checked out in CI. So a CI run can enforce instance-only
PATHS but cannot check tokens, and it says so rather than implying a full pass.
The commit hook — where the overlay is present — is what catches tokens.

Wired via the repo's pre-commit hook and quality.yml.
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import subprocess
import sys

# Instance-only slots — org-agnostic, always blocked in the public engine.
DEFAULT_BLOCKED_PATHS = ["context/brand.md", "context/brand-tokens.css", "marketing/",
                          # An operator's injection tripwires: publishing them
                          # tells an attacker exactly what to phrase around.
                          "context/guardrail-patterns.json"]

# Files allowed to name tokens/paths because they document the mechanism.
ALLOW = {
    "scripts/check_public_hygiene.py",
    "context/brand.example.md",
    "context/brand-tokens.example.css",
    "context/hygiene.example.json",
    ".gitignore",
    ".pre-commit-config.yaml",
    "HANDOFF.md",
}


def find_overlay() -> str | None:
    """Resolve the active private overlay: $AICP_INSTANCE, else a *-instance sibling."""
    env = os.environ.get("AICP_INSTANCE")
    if env and os.path.isdir(env):
        return env
    parent = os.path.dirname(os.getcwd())
    for d in sorted(glob.glob(os.path.join(parent, "*-instance"))):
        if os.path.isdir(d):
            return d
    return None


def load_config() -> dict:
    candidates = []
    overlay = find_overlay()
    if overlay:
        candidates.append(os.path.join(overlay, "hygiene.json"))
    candidates += ["context/hygiene.json", "context/hygiene.example.json"]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
    return {}


def staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True,
    ).stdout
    return [line for line in out.splitlines() if line]


def tracked_files() -> list[str]:
    """Every file git tracks — i.e. exactly what a public cloner receives."""
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]


def main() -> int:
    ap = argparse.ArgumentParser(description="Keep a private org identity out of this engine.")
    ap.add_argument("--tree", action="store_true",
                    help="scan every TRACKED file (CI mode) rather than staged files")
    args = ap.parse_args()

    cfg = load_config()
    forbidden = [t for t in (list(cfg.get("forbidden_hex", [])) +
                             list(cfg.get("forbidden_text", []))) if t]
    blocked = cfg.get("blocked_paths", DEFAULT_BLOCKED_PATHS)
    files = tracked_files() if args.tree else staged_files()
    scope = "tracked" if args.tree else "staged"
    problems: list[str] = []
    for path in files:
        if any(path == p or path.startswith(p) for p in blocked):
            problems.append(f"{path}: instance-only path staged into the public engine")
            continue
        if path in ALLOW or not forbidden:
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for marker in forbidden:
            if marker in text:
                problems.append(
                    f"{path}: contains '{marker}' — private identity belongs in "
                    f"your *-instance overlay, not the public engine"
                )
    if problems:
        print("Public-hygiene guard FAILED:")
        for p in problems:
            print("  -", p)
        print("\nFix: move the value to your private overlay, or use a neutral token.")
        return 1

    # Report WHAT WAS CHECKED, never a bare "ok". An empty scan and a clean scan
    # are different facts, and the bare version is how this passed CI while
    # inspecting nothing.
    if forbidden:
        detail = f"{len(forbidden)} token(s) + {len(blocked)} blocked path(s)"
    else:
        detail = (f"{len(blocked)} blocked path(s) ONLY — no token list available "
                  f"(private overlay not present), so tokens were NOT checked")
    print(f"Public-hygiene guard: {len(files)} {scope} file(s), {detail}. Clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
