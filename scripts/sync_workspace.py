#!/usr/bin/env python3
"""Sync the private overlay's workspace content into the engine's runtime tree.

THE SPLIT. Runner mounts are relative paths under the engine's `./workspace`, so
that is where runtime context has to physically live. But `workspace/` is tracked
and ships in the public snapshot, and the engine's .gitignore now excludes real
scope subtrees — which left real context files versioned nowhere at all.

So the AUTHORITATIVE copy lives in the overlay (`<overlay>/workspace/...`, a
private repo with real history), and this copies it into the engine's runtime
tree. One direction only: overlay -> engine. The engine copy is a deployment
artifact, not a place to edit.

WHY THERE IS A MANIFEST. Blind copying silently destroys work — someone edits
`workspace/scopes/x/CLAUDE.md` in place, the next sync overwrites it, and the
only evidence is that the file got worse. `workspace/.sync-manifest.json` records
the hash written at the last sync, which makes the three cases distinguishable:

    dest missing              -> copy
    dest matches the manifest -> ours, safe to overwrite
    dest differs              -> a LOCAL EDIT. Refuse and say so.

Refusing is the point. Without the manifest this script cannot tell an edit it
should preserve from one it should replace, and "probably fine" is how you lose
the one file someone cared about.

    python scripts/sync_workspace.py            # sync, refusing over local edits
    python scripts/sync_workspace.py --check    # report drift, change nothing
    python scripts/sync_workspace.py --force    # overwrite local edits too
    python scripts/sync_workspace.py --prune    # also delete files the overlay dropped

Exits non-zero on drift under --check, or on a refusal. No overlay -> exit 0
with a SKIP, so this is safe in CI, where there is nothing to sync.
"""
import hashlib, io, json, os, shutil, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_source                                        # noqa: E402

DEST_ROOT = os.path.join(ROOT, "workspace")
# NOT inside workspace/. That directory is bind-mounted into the runners, so a
# manifest written there shows up in `ls /workspace` for every agent — host
# bookkeeping appearing as context, which is the same class of mistake as
# mounting a directory an agent has no business reading.
MANIFEST = os.path.join(ROOT, ".aicp", "sync-manifest.json")

CHECK = "--check" in sys.argv
FORCE = "--force" in sys.argv
PRUNE = "--prune" in sys.argv


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# Double-encoded UTF-8: text read as cp1252 and written back as UTF-8, which is
# what happens the moment a Windows tool touches these files without an explicit
# encoding (`Get-Content` + `WriteAllText` did exactly this to the first context
# file synced here — every em dash became "â€"" and nothing complained). Worth
# catching because the damage is silent: the file still parses, still renders,
# and the agent just reads slightly corrupted instructions forever.
MOJIBAKE = [b"\xc3\xa2\xc2\x80", b"\xc3\xa2\xe2\x82\xac", b"\xc3\x83\xc2"]


def looks_double_encoded(path):
    if not path.lower().endswith((".md", ".txt", ".json", ".yml", ".yaml")):
        return False
    with open(path, "rb") as f:
        blob = f.read()
    return any(m in blob for m in MOJIBAKE)


def load_manifest():
    try:
        with io.open(MANIFEST, encoding="utf-8") as f:
            return json.load(f).get("files", {})
    except (OSError, ValueError):
        return {}


def save_manifest(files):
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    with io.open(MANIFEST, "w", encoding="utf-8", newline="\n") as f:
        json.dump({
            "_note": "Written by scripts/sync_workspace.py. Hashes of the files it "
                     "last copied from the overlay, so a later sync can tell its own "
                     "output from someone's local edit. Delete this and the script "
                     "must treat every existing file as possibly-edited.",
            "files": files,
        }, f, indent=1, sort_keys=True)
        f.write("\n")


def main():
    overlay = scope_source.find_overlay(ROOT)
    src_root = os.path.join(overlay, "workspace") if overlay else None
    if not src_root or not os.path.isdir(src_root):
        print(f"SKIP  no overlay workspace to sync from ({src_root or 'no overlay found'})")
        return 0
    print(f"source: {src_root}\ntarget: {DEST_ROOT}")

    manifest = load_manifest()
    sources = {}
    for dirpath, _dirs, filenames in os.walk(src_root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            sources[os.path.relpath(full, src_root).replace(os.sep, "/")] = full

    copied, blocked, unchanged, pruned, stale = [], [], [], [], []
    garbled = [rel for rel, src in sorted(sources.items()) if looks_double_encoded(src)]
    new_manifest = dict(manifest)

    for rel, src in sorted(sources.items()):
        if rel in garbled:
            continue          # never propagate known-corrupt bytes
        dest = os.path.join(DEST_ROOT, rel.replace("/", os.sep))
        src_hash = sha(src)
        if os.path.exists(dest):
            dest_hash = sha(dest)
            if dest_hash == src_hash:
                unchanged.append(rel)
                new_manifest[rel] = src_hash
                continue
            # Differs from the overlay. Overwrite ONLY if the manifest says this
            # is byte-for-byte what we ourselves last wrote. Anything else — a
            # local edit, or a file that predates the manifest — is content we
            # cannot account for, and unaccounted content does not get destroyed.
            ours = manifest.get(rel) == dest_hash
            if not ours and not FORCE:
                blocked.append(rel)
                continue
        if CHECK:
            stale.append(rel)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # MUST write in place. gen_compose emits FILE bind mounts for slot files
        # (`./workspace/.../CLAUDE.md:/workspace/.../CLAUDE.md`), and a file bind
        # mount follows the INODE, not the path. copy2 truncates and rewrites the
        # existing file, so a running container sees the new content immediately.
        # The usual "atomic write" improvement — temp file plus os.replace — would
        # give the destination a new inode, and every running container would keep
        # reading the OLD content forever with nothing to indicate it. Verified by
        # syncing an edit and grepping for it inside both live runners.
        shutil.copy2(src, dest)
        copied.append(rel)
        new_manifest[rel] = src_hash

    # Files we previously synced that the overlay no longer has.
    for rel in sorted(set(manifest) - set(sources)):
        dest = os.path.join(DEST_ROOT, rel.replace("/", os.sep))
        if not os.path.exists(dest):
            new_manifest.pop(rel, None)
            continue
        if sha(dest) != manifest[rel]:
            print(f"  KEEP    {rel} — dropped from the overlay but edited here; not deleting")
            continue
        if PRUNE and not CHECK:
            os.remove(dest)
            new_manifest.pop(rel, None)
            pruned.append(rel)
        else:
            stale.append(rel + " (removed upstream; --prune to delete)")

    for rel in copied:
        print(f"  copied  {rel}")
    for rel in pruned:
        print(f"  pruned  {rel}")
    for rel in stale:
        print(f"  STALE   {rel}")
    for rel in blocked:
        print(f"  BLOCKED {rel} — differs from the overlay AND from what we last wrote. "
              "Edit the overlay copy (it is authoritative), or --force to discard this one.")
    for rel in garbled:
        print(f"  MANGLED {rel} — the OVERLAY copy contains double-encoded UTF-8. "
              "A Windows tool rewrote it without an explicit encoding; fix the source, "
              "syncing will only propagate it.")
    if unchanged:
        print(f"  {len(unchanged)} file(s) already in sync")

    if not CHECK:
        save_manifest(new_manifest)

    if blocked:
        print(f"\nREFUSED: {len(blocked)} local edit(s) would have been overwritten.")
        return 1
    if garbled:
        print(f"\nMANGLED: {len(garbled)} source file(s) are double-encoded.")
        return 1
    if CHECK and stale:
        print(f"\nOUT OF SYNC: {len(stale)} file(s). Run scripts/sync_workspace.py")
        return 1
    print("\nWorkspace in sync with the overlay.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
