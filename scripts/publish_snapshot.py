#!/usr/bin/env python3
"""Publish a repo as a single-commit public snapshot — by ALLOWLIST, not by copy.

`change-safety.md` already said this: *"Publishing is a list, not a side effect …
make publication an explicit allowlist the deploy script reads, so adding
something to the site is a deliberate edit rather than a consequence of where a
file was saved."* The rule existed and the mechanism did not, and the first
public snapshot of one repo duly shipped its own market analysis, its open-gap
list, and an internal handover doc — none of which anyone chose to publish.

WHAT IT DOES

  1. Takes the tracked tree, minus `.publish-exclude`.
  2. Runs the gates ON THAT TREE — not on the working copy, because the working
     copy is not what ships.
  3. Refuses on any failure, including a link that dangles only after exclusion.
  4. Writes ONE orphan commit and force-pushes it.

WHY FORCE-PUSH IS SAFE HERE AND NOWHERE ELSE. The target's history is *designed*
to be a single commit — there is no history to destroy. That is categorically
different from rewriting a working repo, which this refuses to do: if the target
remote matches the source's own `origin`, it aborts.

    python scripts/publish_snapshot.py --to git@github.com:me/thing-public.git --dry-run
    python scripts/publish_snapshot.py --to https://github.com/me/thing-public.git

Pure stdlib.
"""
from __future__ import annotations
import argparse, fnmatch, os, shutil, subprocess, sys, tempfile

EXCLUDE_FILE = ".publish-exclude"


def git(cwd, *args, check=False):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:200]}")
    return r


def load_exclude(root):
    """gitignore-style patterns, one per line, # comments.

    A MISSING file is an error, not an empty list. Publishing everything because
    the allowlist config was absent is precisely the failure this exists to
    prevent, and it would look identical to a deliberate 'publish it all'.
    """
    p = os.path.join(root, EXCLUDE_FILE)
    if not os.path.isfile(p):
        raise SystemExit(
            f"REFUSING: no {EXCLUDE_FILE} in {root}.\n"
            f"  Publishing every tracked file because the exclusion list is "
            f"missing is the exact accident this tool exists to prevent.\n"
            f"  Create it — an empty file means 'publish everything', said out loud.")
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#")[0].strip()
            if line:
                out.append(line)
    return out


def excluded(rel, patterns):
    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.rstrip("/") + "/*"):
            return pat
        if rel == pat.rstrip("/") or rel.startswith(pat.rstrip("/") + "/"):
            return pat
    return None


def build_tree(root, dest, patterns):
    files = [f for f in git(root, "ls-files", check=True).stdout.splitlines() if f.strip()]
    kept, dropped = [], []
    for rel in files:
        pat = excluded(rel, patterns)
        if pat:
            dropped.append((rel, pat)); continue
        src = os.path.join(root, *rel.split("/"))
        dst = os.path.join(dest, *rel.split("/"))
        if not os.path.isfile(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        kept.append(rel)
    return kept, dropped


def run_gates(root, tree, overlay=None):
    """Gates run against the SNAPSHOT, because that is what ships.

    Running them against the working copy would pass on files the snapshot does
    not contain and miss links that dangle only once something is excluded.

    The tree is `git init`+`add`ed FIRST. A --tree gate enumerates via
    `git ls-files`, so against a plain directory it scans ZERO files and reports
    clean — the same vacuous pass this repo's own AGENTS.md warns about
    ("it would pass while checking nothing"). The first version of this function
    did exactly that, and the fix is asserting a non-zero file count below
    rather than trusting the exit code.
    """
    git(tree, "init", "-q", "-b", "snapshot")
    git(tree, "add", "-A")
    n = len([x for x in git(tree, "ls-files").stdout.splitlines() if x.strip()])
    if n == 0:
        return [("snapshot is empty", False,
                 "git ls-files found nothing — every --tree gate would pass "
                 "vacuously")]
    env = dict(os.environ)
    if overlay:
        env["AICP_INSTANCE"] = overlay
    results = [("snapshot indexed", True, f"{n} file(s) visible to --tree gates")]
    # Run the SNAPSHOT'S OWN COPY of each gate, not the source repo's.
    #
    # This is not a nicety. check_links.py derives its root from `__file__`, so
    # invoking the source copy with cwd=tree scanned THE SOURCE and reported
    # clean while the snapshot had six dangling links — the ones left behind by
    # excluding docs/plans/. The gate ran, passed, and proved nothing about what
    # shipped. Public CI found them instead, which is the wrong place.
    #
    # A gate that resolves its own root must be executed from inside the tree it
    # is meant to judge. Falling back to the source copy would reintroduce
    # exactly that, so a missing script in the snapshot is reported as skipped.
    for name, rel, extra in (
        ("public hygiene", "scripts/check_public_hygiene.py", ["--tree"]),
        ("PII (tree, public/P1)", "scripts/pii_scan.py",
         [".", "--tree", "--visibility", "public", "--max-tier", "P1"]),
        ("doc links", "scripts/check_links.py", []),
    ):
        script = os.path.join(tree, *rel.split("/"))
        argv = [sys.executable, script, *extra]
        if not os.path.isfile(script):
            results.append((name, None,
                            "not in the snapshot — skipped (the source copy is "
                            "deliberately NOT used: it would judge the wrong tree)"))
            continue
        r = subprocess.run(argv, cwd=tree, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env)
        results.append((name, r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1][:150]
                        if (r.stdout or r.stderr).strip() else ""))
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description="Publish a single-commit snapshot by allowlist.")
    ap.add_argument("--to", required=True, help="target remote URL (a *-public snapshot repo)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)

    origin = git(root, "remote", "get-url", "origin").stdout.strip()
    def norm(u):
        return u.rstrip("/").removesuffix(".git").replace("git@github.com:", "github.com/") \
                .replace("https://github.com/", "github.com/").lower()
    if origin and norm(origin) == norm(a.to):
        raise SystemExit(
            f"REFUSING: --to is this repo's own origin ({origin}).\n"
            f"  This force-pushes a single orphan commit. Against a snapshot repo "
            f"that destroys nothing; against your working repo it destroys "
            f"everything.")

    patterns = load_exclude(root)
    tmp = tempfile.mkdtemp(prefix="snapshot-")
    tree = os.path.join(tmp, "tree")
    os.makedirs(tree)
    kept, dropped = build_tree(root, tree, patterns)

    print(f"  {len(kept)} file(s) to publish · {len(dropped)} excluded")
    by_pat = {}
    for rel, pat in dropped:
        by_pat.setdefault(pat, []).append(rel)
    for pat, rels in sorted(by_pat.items()):
        print(f"    excluded by {pat!r}: {len(rels)} file(s)")

    # The overlay supplies the forbidden-token list. Without it the hygiene gate
    # checks blocked PATHS only and says so — which is a weaker check silently
    # substituted for a stronger one unless the operator is told.
    overlay = os.environ.get("AICP_INSTANCE")
    if not overlay:
        sib = os.path.join(os.path.dirname(root),
                           os.path.basename(root).split("-")[0] + "-instance")
        for cand in (sib, *[os.path.join(os.path.dirname(root), d)
                            for d in os.listdir(os.path.dirname(root))
                            if d.endswith("-instance")]):
            if os.path.isdir(cand):
                overlay = cand
                break
    print(f"  overlay for token checks: {overlay or 'NONE — tokens will NOT be checked'}")
    print("  gates, run against the snapshot:")
    ok = True
    for name, passed, detail in run_gates(root, tree, overlay):
        mark = "—" if passed is None else ("✓" if passed else "✗")
        print(f"    {mark} {name:24s} {detail}")
        if passed is False:
            ok = False
    if not ok:
        print("\n  REFUSING to publish: a gate failed on the snapshot.\n"
              "  A gate that passes on the working copy proves nothing about what "
              "ships.", file=sys.stderr)
        return 1

    sha = git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    msg = a.message or f"Public snapshot ({sha})"
    if a.dry_run:
        print(f"\n  [dry-run] would force-push one commit {msg!r} to {a.to}")
        return 0

    # Already initialised by run_gates on branch "snapshot"; move to the target
    # branch name rather than re-initialising over it.
    git(tree, "checkout", "-q", "-B", a.branch, check=True)
    git(tree, "add", "-A", check=True)
    subprocess.run(["git", "-C", tree, "-c", "commit.gpgsign=false",
                    "commit", "-q", "-m", msg], check=True)
    r = subprocess.run(["git", "-C", tree, "push", "--force", a.to,
                        f"{a.branch}:{a.branch}"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"  push FAILED: {r.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    print(f"\n  published {len(kept)} file(s) as one commit -> {a.to}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
