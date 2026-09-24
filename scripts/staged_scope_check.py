#!/usr/bin/env python3
"""Pre-commit gate: refuse to commit files this agent did not touch.

WHY THIS EXISTS. `git add -A` / `git add <dir>` stages everything under the
path, including another writer's uncommitted work. In this project that has
happened four recorded times:

  - a `git add docs/` swept 660 lines of another agent's untracked ADR into a
    commit whose message described neither, and pushed it
  - a `git add -A` swept in an unrelated CI tool — caught only because the PII
    gate happened to block it
  - a `git add -A` committed two .pyc files, contributing to a gate that then
    failed for seven consecutive commits
  - a `git add tests/` committed HALF of someone's in-flight change: their new
    test, without the source function it calls, breaking their build

The rule against it has been written in change-safety.md the whole time, in a
file the agent had open on the day it broke it twice. **A rule in a document is
not a mechanism.** Every other recurring failure here got fixed by becoming a
gate — PII, public hygiene, doc links, ADR shape, workflow lint. This one never
did, and it is the only house rule left running on intention alone.

THIS IS A BACKSTOP, NOT THE FIX. Read this before extending it.

The FOREIGN case below only arises when two writers share one working tree —
which `concurrency-and-branching.md` already forbids ("Never two agents in one
working tree"), and which this project already solves elsewhere: the fan-out
lane gives each agent its own `git worktree`. **Isolation removes the failure;
this file only notices it.** If you are about to extend the FOREIGN logic, the
real answer is a worktree per agent and you are polishing a symptom.

It stays because the un-isolated case is real today — a human and an agent
writing in one tree is the common shape — and a cheap backstop under a rule
broken four times earns its keep. It should shrink over time, not grow.

WHAT IT CHECKS. Two narrow things, both mechanical, neither firing on ordinary
work:

  FOREIGN   a staged file that was ALREADY modified when this agent started
            and that the agent never edited. Someone else's in-flight work —
            a symptom of missing isolation, per above.

  IGNORED   a staged file that .gitignore matches while git tracks it anyway,
            so the ignore rule silently does nothing. NOT a symptom of anything
            else, and worth keeping on its own: it made a render gate
            unpassable for seven commits. Implemented as git's own idiom,
            `git ls-files -i -c --exclude-standard`.

Deliberately NOT checked: files the agent created via a shell command or a
generator. They are legitimate and constant here, and flagging them would make
the gate noisy, and a noisy gate gets removed.

    python scripts/staged_scope_check.py            # pre-commit
    python scripts/staged_scope_check.py --explain  # why each file passed

Override with SCOPE_CHECK_ALLOW=path1,path2 when you genuinely mean to commit
another writer's file — an explicit, visible act rather than a silent default.

Pure stdlib.
"""
from __future__ import annotations
import argparse, os, subprocess, sys


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def repo_root():
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else None


def read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return {l.strip() for l in fh if l.strip()}
    except OSError:
        return set()


def staged(root):
    r = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACM")
    return [p for p in r.stdout.splitlines() if p.strip()]


def ignored(root, paths):
    """Staged paths that .gitignore matches while git tracks them anyway.

    `git ls-files -i -c --exclude-standard` is git's own idiom for exactly this
    question. The first version piped the staged list through `check-ignore
    --no-index --stdin` — same answer, verified, but reimplementing plumbing git
    already exposes. Prefer the built-in.
    """
    r = git(root, "ls-files", "-i", "-c", "--exclude-standard")
    tracked_ignored = {p for p in r.stdout.splitlines() if p.strip()}
    return [p for p in paths if p in tracked_ignored]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Refuse to commit files this agent did not touch.")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--root", default=None)
    a = ap.parse_args(argv)

    root = a.root or repo_root()
    if not root:
        return 0                      # not a git repo: nothing to guard

    paths = staged(root)
    if not paths:
        return 0

    gitdir = os.path.join(root, ".git")
    touched = read_lines(os.path.join(gitdir, "agent-touched"))
    baseline = read_lines(os.path.join(gitdir, "agent-baseline"))
    allow = {p.strip() for p in os.environ.get("SCOPE_CHECK_ALLOW", "").split(",") if p.strip()}

    if not baseline and not touched:
        # No ledger: a human at a terminal, or a session that predates the hook.
        # Say so rather than passing silently — "checked" and "nothing to check
        # with" must not look the same.
        print("scope check: no agent ledger in .git/ — not enforced this commit "
              "(hook not installed, or a manual commit).")
        return 0

    raw_foreign = [p for p in paths if p in baseline and p not in touched]
    raw_ign = ignored(root, paths)
    foreign = [p for p in raw_foreign if p not in allow]
    ign = [p for p in raw_ign if p not in allow]
    # What the override actually suppressed. Reported, because a gate that says
    # "all yours" after being overridden is lying in the transcript — and the
    # transcript is the record someone reads later to find out what happened.
    waived = sorted(set(raw_foreign + raw_ign) & allow)

    if a.explain:
        for p in paths:
            why = ("FOREIGN" if p in foreign else "IGNORED" if p in ign
                   else "edited" if p in touched else "ok (new this session)")
            print(f"  {why:8s} {p}")

    if not foreign and not ign:
        if waived:
            print(f"Scope check: {len(paths)} staged file(s) — {len(waived)} "
                  f"WAIVED by SCOPE_CHECK_ALLOW: {', '.join(waived)}")
        else:
            print(f"Scope check: {len(paths)} staged file(s), all yours. ✓")
        return 0

    print("\nSTAGED FILES THAT ARE NOT YOURS TO COMMIT\n", file=sys.stderr)
    if foreign:
        print("  Already modified before this session started, and never edited "
              "here — someone else's in-flight work:", file=sys.stderr)
        for p in foreign:
            print(f"    {p}", file=sys.stderr)
        print("\n  Staging these either takes their change or, worse, takes HALF "
              "of it.\n  Unstage:  git restore --staged " + " ".join(foreign),
              file=sys.stderr)
    if ign:
        print("\n  Matched by .gitignore but tracked anyway, so the ignore rule "
              "does nothing:", file=sys.stderr)
        for p in ign:
            print(f"    {p}", file=sys.stderr)
        print("\n  Untrack:  git rm --cached " + " ".join(ign), file=sys.stderr)
    print("\n  If you genuinely mean to commit these, say so explicitly:\n"
          "    SCOPE_CHECK_ALLOW=" + ",".join(foreign + ign) + " git commit …",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
