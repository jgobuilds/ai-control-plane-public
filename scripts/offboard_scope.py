#!/usr/bin/env python3
"""Provable tenant offboarding — right-to-deletion with an evidence trail.

Deletes a scope's entire footprint:
  - its workspace subtree   workspace/<workspaceRoot>/<chain>/
  - its token vault         vault/<scope>.json
and writes a DELETION CERTIFICATE to
    audit/deletions/<scope>-<utc-timestamp>.json
containing the scope, its chain, the operator, a UTC timestamp, and a MANIFEST of
every file removed with its byte size and SHA-256 — all computed BEFORE deletion,
so the certificate is a verifiable record of exactly what was destroyed.

DRY-RUN by default (shows the manifest + where the certificate would go, deletes
nothing, writes no certificate). `--confirm` executes.

Refuses to offboard a scope that still has child scopes (offboard leaves first)
unless `--recursive`, in which case the scope AND all its descendants are removed
(their vault files too) and all appear in one certificate.

NEVER touches the audit ledger (audit/decisions.jsonl); it only WRITES a new file
under audit/deletions/. Pure stdlib. Windows-friendly (os.path only).

    python scripts/offboard_scope.py tenant-a                     # dry-run
    python scripts/offboard_scope.py tenant-a --confirm --operator jon
    python scripts/offboard_scope.py consulting --recursive --confirm
"""
import os, sys, json, argparse, hashlib, datetime, shutil


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_scopes(root):
    with open(os.path.join(root, "context", "scopes.json"), encoding="utf-8") as f:
        return json.load(f)


def chain(scopes, name):
    out, cur, guard = [], name, 0
    while cur:
        guard += 1
        if guard > 64:
            raise SystemExit("scope tree cycle at " + name)
        out.insert(0, cur)
        cur = scopes["nodes"][cur].get("parent")
    return out


def children(scopes, name):
    return [k for k, v in scopes["nodes"].items() if v.get("parent") == name]


def descendants(scopes, name):
    """All scopes strictly below `name` (depth-first)."""
    out = []
    for c in children(scopes, name):
        out.append(c)
        out.extend(descendants(scopes, c))
    return out


def scope_dir(root, scopes, chain_list):
    ws_root = scopes.get("workspaceRoot", "scopes")
    return os.path.join(root, "workspace", ws_root, *chain_list)


def vault_path(root, scope):
    return os.path.join(root, "vault", f"{scope}.json")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def manifest_entry(root, path):
    return {
        "path": os.path.relpath(path, root).replace(os.sep, "/"),
        "size": os.path.getsize(path),
        "sha256": sha256_file(path),
    }


def collect_targets(root, scopes, name, recursive):
    """Return (dirs_to_remove, vault_files_to_remove, manifest).

    Manifest hashes EVERY file that will be destroyed (subtree files + vault
    files), computed before anything is touched.
    """
    scopes_to_purge = [name] + (descendants(scopes, name) if recursive else [])

    # The subtree at `name` already nests every descendant on disk, so removing
    # the one top directory covers all descendant workspace content.
    top_dir = scope_dir(root, scopes, chain(scopes, name))
    dirs_to_remove = [top_dir] if os.path.isdir(top_dir) else []

    manifest = []
    if os.path.isdir(top_dir):
        for dirpath, _dirnames, filenames in os.walk(top_dir):
            for fn in sorted(filenames):
                manifest.append(manifest_entry(root, os.path.join(dirpath, fn)))

    vault_files = []
    for s in scopes_to_purge:
        vp = vault_path(root, s)
        if os.path.isfile(vp):
            vault_files.append(vp)
            manifest.append(manifest_entry(root, vp))

    manifest.sort(key=lambda m: m["path"])
    return dirs_to_remove, vault_files, manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description="Provable tenant offboarding (right-to-deletion).")
    ap.add_argument("scope", help="scope to offboard")
    ap.add_argument("--confirm", action="store_true", help="execute (default: dry-run)")
    ap.add_argument("--recursive", action="store_true", help="also offboard all descendant scopes")
    ap.add_argument("--operator", help="operator name for the certificate (default: $USER / $USERNAME)")
    ap.add_argument("--root", default=repo_root(), help="repo root (default: two levels up from this script)")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    scopes = load_scopes(root)
    name = args.scope
    if name not in scopes["nodes"]:
        raise SystemExit(f"unknown scope '{name}'")

    kids = children(scopes, name)
    if kids and not args.recursive:
        raise SystemExit(
            f"scope '{name}' has child scope(s): {', '.join(sorted(kids))}. "
            f"Offboard leaves first, or pass --recursive to offboard the whole subtree.")

    operator = args.operator or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    ts = datetime.datetime.now(datetime.timezone.utc)
    ch = chain(scopes, name)

    dirs_to_remove, vault_files, manifest = collect_targets(root, scopes, name, args.recursive)

    purged = [name] + (descendants(scopes, name) if args.recursive else [])
    total_bytes = sum(m["size"] for m in manifest)

    mode = "CONFIRM (executing)" if args.confirm else "DRY-RUN (no changes)"
    print(f"Offboard scope '{name}' — {mode}")
    print(f"  chain    : {'/'.join(ch)}")
    print(f"  recursive: {args.recursive}  (scopes purged: {', '.join(purged)})")
    print(f"  operator : {operator}")
    print(f"  manifest : {len(manifest)} file(s), {total_bytes}B")
    for m in manifest:
        print(f"    - {m['path']}  ({m['size']}B, sha256:{m['sha256'][:16]}…)")
    if not dirs_to_remove and not vault_files:
        print("  (nothing on disk to remove — scope has no workspace subtree or vault)")

    certificate = {
        "type": "deletion-certificate",
        "scope": name,
        "chain": ch,
        "recursive": args.recursive,
        "scopesPurged": purged,
        "operator": operator,
        "ts": ts.isoformat(),
        "workspaceSubtree": os.path.relpath(scope_dir(root, scopes, ch), root).replace(os.sep, "/"),
        "vaultFiles": [os.path.relpath(v, root).replace(os.sep, "/") for v in vault_files],
        "fileCount": len(manifest),
        "totalBytes": total_bytes,
        "manifest": manifest,
        "dryRun": not args.confirm,
    }

    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    cert_dir = os.path.join(root, "audit", "deletions")
    cert_path = os.path.join(cert_dir, f"{name}-{stamp}.json")

    if not args.confirm:
        print(f"\nDRY-RUN: no files deleted, no certificate written.")
        print(f"Certificate WOULD be written to: "
              f"{os.path.relpath(cert_path, root).replace(os.sep, '/')}")
        print("Re-run with --confirm to execute.")
        return 0

    # --- execute: delete first (data is gone), then write the certificate ---
    for d in dirs_to_remove:
        shutil.rmtree(d)
    for v in vault_files:
        os.remove(v)

    os.makedirs(cert_dir, exist_ok=True)
    with open(cert_path, "w", encoding="utf-8") as f:
        json.dump(certificate, f, indent=2)
        f.write("\n")

    print(f"\nDeleted {len(manifest)} file(s) across {len(purged)} scope(s).")
    print(f"Deletion certificate written: {cert_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
