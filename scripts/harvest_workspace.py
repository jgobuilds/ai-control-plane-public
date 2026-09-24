#!/usr/bin/env python3
"""Harvest agent work products from the engine workspace back into the overlay.

THE GAP THIS CLOSES. `sync_workspace.py` runs one direction — overlay to engine —
because the overlay is authoritative for CONTEXT. But agents write too: a real
`plan` run produced `internal/n8n/weekly-eval-metrics-drift.json`, a genuine
artifact that lived in the engine's `workspace/`, which `.gitignore` excludes at
`workspace/scopes/*`, and did not exist in the overlay at all. It was versioned
NOWHERE. A container rebuild or a `--force` sync would have taken it silently.

This is the return path, and it is deliberately not a sync. Agent output entering
a git repo is a review step: this REPORTS by default, copies only with `--apply`,
and never commits. The overlay's own PII gate runs at commit time, which is the
last line rather than this one.

    python scripts/harvest_workspace.py            # report candidates, change nothing
    python scripts/harvest_workspace.py --apply    # copy them into the overlay
    python scripts/harvest_workspace.py --apply --force   # also overwrite conflicts

FOUR STATES, and telling them apart is the whole job. Comparing engine to overlay
alone cannot: a difference means either the agent wrote something new or the
overlay moved ahead and the engine is stale, and copying in the second case
would silently revert a human edit with an agent's older copy.

  NEW        in the engine, absent from the overlay          -> harvest
  MODIFIED   differs, and the engine copy changed since the
             last sync (so an agent touched it)              -> harvest
  STALE      differs, but the engine copy still matches what
             sync last wrote — the OVERLAY moved ahead       -> skip, run sync
  CONFLICT   both sides changed since the last sync          -> refuse

Never harvested: runtime work products (`ingest/`, `deliverables/`, `.fanout/`)
and scratch files. Those are excluded from git on purpose; hoovering them into a
repo would undo that decision by accident.
"""
import argparse, hashlib, io, json, os, shutil, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_source                                        # noqa: E402

SRC_ROOT = os.path.join(ROOT, "workspace")
MANIFEST = os.path.join(ROOT, ".aicp", "sync-manifest.json")

# Runtime paths git already refuses; harvesting them would launder that refusal.
EXCLUDE_DIRS = {"ingest", "deliverables", ".fanout", "__pycache__", "node_modules", "tmp"}
EXCLUDE_SUFFIX = (".tmp", ".pyc", ".log", ".lock")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest():
    try:
        with io.open(MANIFEST, encoding="utf-8") as f:
            return json.load(f).get("files", {})
    except (OSError, ValueError):
        return {}


def engine_tracked():
    """Paths under workspace/ that the ENGINE versions itself.

    These already have a home — the sample scope tree, the workspace-root
    CLAUDE.md, the shared skills. Harvesting them would copy engine-owned files
    into the overlay and create a second, diverging copy of each. Git is the
    right authority for "who owns this": tracked here means not ours to take.
    The first dry run flagged all eight of them, which is how this rule got
    written.
    """
    import subprocess
    try:
        out = subprocess.run(["git", "ls-files", "workspace"], cwd=ROOT, check=True,
                             capture_output=True, text=True, encoding="utf-8").stdout
    except (OSError, subprocess.CalledProcessError):
        return set()          # no git: fall back to harvesting everything eligible
    return {p[len("workspace/"):] for p in out.split() if p.startswith("workspace/")}


def walk(root, skip=frozenset()):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in files:
            if fn.endswith(EXCLUDE_SUFFIX) or fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel in skip:
                continue
            out[rel] = full
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Harvest agent output into the overlay.")
    ap.add_argument("--apply", action="store_true", help="actually copy (default: report only)")
    ap.add_argument("--force", action="store_true", help="also overwrite CONFLICT files")
    args = ap.parse_args(argv)

    overlay = scope_source.find_overlay(ROOT)
    if not overlay:
        print("SKIP  no overlay found — nothing to harvest into.")
        return 0
    dest_root = os.path.join(overlay, "workspace")
    print(f"engine : {SRC_ROOT}\noverlay: {dest_root}\n")

    manifest = load_manifest()
    owned = engine_tracked()
    if owned:
        print(f"  ({len(owned)} engine-tracked file(s) under workspace/ skipped — "
              "they are versioned in the engine already)\n")
    engine, buckets = walk(SRC_ROOT, skip=owned), {"NEW": [], "MODIFIED": [], "STALE": [], "CONFLICT": []}

    for rel, src in sorted(engine.items()):
        dest = os.path.join(dest_root, rel.replace("/", os.sep))
        src_hash = sha(src)
        if not os.path.exists(dest):
            buckets["NEW"].append((rel, src, dest, src_hash))
            continue
        dest_hash = sha(dest)
        if dest_hash == src_hash:
            continue
        synced = manifest.get(rel)
        engine_moved = synced is None or src_hash != synced
        overlay_moved = synced is not None and dest_hash != synced
        if engine_moved and overlay_moved:
            buckets["CONFLICT"].append((rel, src, dest, src_hash))
        elif engine_moved:
            buckets["MODIFIED"].append((rel, src, dest, src_hash))
        else:
            buckets["STALE"].append((rel, src, dest, src_hash))

    for kind in ("NEW", "MODIFIED", "CONFLICT", "STALE"):
        for rel, *_ in buckets[kind]:
            print(f"  {kind:<9} {rel}")
    if not any(buckets.values()):
        print("  nothing to harvest — the overlay already has everything the engine does")
        return 0

    if buckets["STALE"]:
        print("\n  STALE means the OVERLAY is ahead and the engine copy is the older synced\n"
              "  one. Harvesting those would revert a human edit. Run sync_workspace.py.")
    if buckets["CONFLICT"] and not args.force:
        print("\n  CONFLICT means both sides changed since the last sync. Not guessing which\n"
              "  wins — diff them, or re-run with --force to take the engine copy.")

    take = buckets["NEW"] + buckets["MODIFIED"] + (buckets["CONFLICT"] if args.force else [])
    if not args.apply:
        print(f"\nDRY RUN — {len(take)} file(s) would be copied. Re-run with --apply.")
        return 0

    for rel, src, dest, _h in take:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        print(f"  harvested {rel}")

    print(f"\nHarvested {len(take)} file(s) into the overlay. NOT committed — this is agent\n"
          "output entering version control, so read it before you keep it. The overlay's\n"
          "PII gate runs at commit time; it is the last line, not this one.")
    if take:
        print("\n  cd " + overlay + " && git status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
