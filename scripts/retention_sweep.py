#!/usr/bin/env python3
"""Retention sweep — makes scopes.json `retentionDays` real.

For every scope in context/scopes.json, computes an EFFECTIVE retention window
(node `controls.retentionDays` overrides `levelDefaults[level].retentionDays`;
null means keep forever) and finds work-product files under
    workspace/<workspaceRoot>/<chain>/{ingest,deliverables}   (and their subdirs,
    e.g. deliverables/sent/)
whose mtime is older than that window. DRY-RUN by default (lists what WOULD be
deleted, with age + size); `--apply` actually deletes them.

Vault pruning (best-effort, CONSERVATIVE): after choosing which files a scope
would lose, it inspects the scope's token vault (vault/<scope>.json) and flags a
token as PRUNABLE only when it can prove the token is orphaned — i.e. NEITHER the
token string («EMAIL_1») NOR its underlying raw value appears in ANY remaining
file under that scope. This is a heuristic and deliberately over-cautious: a
token that still appears anywhere is NEVER pruned, so rehydration of surviving
deliverables can never silently break. `--apply` removes the flagged entries from
byToken/byValue but leaves `counters` intact so future tokenization stays
deterministic.

NEVER touches audit/. Missing directories are handled gracefully.

Pure stdlib. Deterministic given a fixed clock. Windows-friendly (os.path only).

    python scripts/retention_sweep.py            # dry-run, all scopes
    python scripts/retention_sweep.py --apply     # actually delete + prune
    python scripts/retention_sweep.py --scope jane
"""
import os, sys, json, argparse, datetime

CONTENT_SUBDIRS = ("ingest", "deliverables")
TOKEN_RE = None  # tokens look like «TYPE_N»; we match by substring, no regex needed


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_scopes(root):
    with open(os.path.join(root, "context", "scopes.json"), encoding="utf-8") as f:
        return json.load(f)


def effective_controls(scopes, name):
    node = scopes["nodes"][name]
    ctl = dict(scopes.get("levelDefaults", {}).get(node["level"], {}))
    ctl.update(node.get("controls", {}))
    return ctl


def chain(scopes, name):
    out, cur, guard = [], name, 0
    while cur:
        guard += 1
        if guard > 64:
            raise SystemExit("scope tree cycle at " + name)
        out.insert(0, cur)
        cur = scopes["nodes"][cur].get("parent")
    return out


def scope_dir(root, scopes, chain_list):
    ws_root = scopes.get("workspaceRoot", "scopes")
    return os.path.join(root, "workspace", ws_root, *chain_list)


def walk_files(directory):
    """Absolute paths of every file under `directory` (recursive). Empty if missing."""
    if not os.path.isdir(directory):
        return
    for dirpath, _dirnames, filenames in os.walk(directory):
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def vault_path(root, scope):
    return os.path.join(root, "vault", f"{scope}.json")


def load_vault(root, scope):
    try:
        with open(vault_path(root, scope), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def sweep_scope(root, scopes, name, now, do_apply):
    """Return a per-scope report dict, or None if the scope keeps forever / no dir."""
    eff = effective_controls(scopes, name)
    retention_days = eff.get("retentionDays")
    if retention_days is None:
        return None  # keep forever — nothing to sweep

    ch = chain(scopes, name)
    base = scope_dir(root, scopes, ch)
    now_ts = now.timestamp()

    all_files, to_delete = [], []
    for sub in CONTENT_SUBDIRS:
        for path in walk_files(os.path.join(base, sub)):
            try:
                mtime = os.path.getmtime(path)
                size = os.path.getsize(path)
            except OSError:
                continue
            age_days = (now_ts - mtime) / 86400.0
            rec = {"path": path, "age_days": age_days, "size": size}
            all_files.append(rec)
            if age_days > retention_days:
                to_delete.append(rec)

    delete_set = {r["path"] for r in to_delete}

    # --- vault prune (conservative) ---
    vault = load_vault(root, name)
    prunable, vault_kept = [], 0
    if vault and isinstance(vault.get("byToken"), dict):
        remaining_paths = [r["path"] for r in all_files if r["path"] not in delete_set]
        remaining_text = "\n".join(read_text(p) for p in remaining_paths)
        for token, value in vault["byToken"].items():
            token_used = token in remaining_text
            value_used = bool(value) and str(value) in remaining_text
            if token_used or value_used:
                vault_kept += 1
            else:
                prunable.append(token)

    # --- apply side effects ---
    deleted_bytes = sum(r["size"] for r in to_delete)
    if do_apply:
        for r in to_delete:
            try:
                os.remove(r["path"])
            except OSError as e:
                print(f"    ! could not delete {r['path']}: {e}", file=sys.stderr)
        if prunable and vault:
            prune_set = set(prunable)
            vault["byToken"] = {t: v for t, v in vault["byToken"].items() if t not in prune_set}
            if isinstance(vault.get("byValue"), dict):
                vault["byValue"] = {k: t for k, t in vault["byValue"].items() if t not in prune_set}
            with open(vault_path(root, name), "w", encoding="utf-8") as f:
                json.dump(vault, f, indent=2)
                f.write("\n")

    return {
        "scope": name,
        "chain": ch,
        "retentionDays": retention_days,
        "isolation": eff.get("isolation"),
        "scanned": len(all_files),
        "to_delete": to_delete,
        "deleted_bytes": deleted_bytes,
        "vault_prunable": prunable,
        "vault_kept": vault_kept,
        "has_vault": vault is not None,
    }


def human_bytes(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Retention sweep for scope work-product + token vaults.")
    ap.add_argument("--apply", action="store_true", help="actually delete files + prune vault (default: dry-run)")
    ap.add_argument("--scope", help="sweep only this scope (default: all scopes)")
    ap.add_argument("--root", default=repo_root(), help="repo root (default: two levels up from this script)")
    ap.add_argument("--now", help="override 'now' as ISO-8601 (testing/determinism)")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    scopes = load_scopes(root)
    now = datetime.datetime.fromisoformat(args.now) if args.now else datetime.datetime.now()

    names = [args.scope] if args.scope else list(scopes["nodes"].keys())
    if args.scope and args.scope not in scopes["nodes"]:
        raise SystemExit(f"unknown scope '{args.scope}'")

    mode = "APPLY (deleting)" if args.apply else "DRY-RUN (no changes)"
    print(f"Retention sweep — {mode}")
    print(f"  root: {root}")
    print(f"  now : {now.isoformat()}\n")

    total_files = total_bytes = total_prunable = 0
    reports = []
    for name in names:
        rep = sweep_scope(root, scopes, name, now, args.apply)
        if rep is None:
            continue
        reports.append(rep)

    active = [r for r in reports if r["to_delete"] or r["vault_prunable"]]
    if not active:
        swept = [r["scope"] for r in reports]
        print("Nothing over retention. Scopes with a retention window:",
              ", ".join(swept) if swept else "(none)")
    for rep in reports:
        verb = "deleted" if args.apply else "would delete"
        n = len(rep["to_delete"])
        if not rep["to_delete"] and not rep["vault_prunable"]:
            continue
        tag = " [silo]" if rep["isolation"] == "silo" else ""
        print(f"scope {rep['scope']}{tag}  (retention {rep['retentionDays']}d, "
              f"{rep['scanned']} file(s) scanned)")
        for r in sorted(rep["to_delete"], key=lambda x: x["path"]):
            rel = os.path.relpath(r["path"], root).replace(os.sep, "/")
            print(f"    {verb}: {rel}  ({r['age_days']:.1f}d old, {r['size']}B)")
        if rep["to_delete"]:
            print(f"    -> {n} file(s), {human_bytes(rep['deleted_bytes'])}")
        if rep["has_vault"]:
            pv = "pruned" if args.apply else "prunable"
            print(f"    vault: {len(rep['vault_prunable'])} {pv}, "
                  f"{rep['vault_kept']} still referenced")
            for t in rep["vault_prunable"]:
                print(f"      - {t}")
        print()
        total_files += n
        total_bytes += rep["deleted_bytes"]
        total_prunable += len(rep["vault_prunable"])

    print(f"TOTAL: {total_files} file(s), {human_bytes(total_bytes)}, "
          f"{total_prunable} vault entr{'y' if total_prunable == 1 else 'ies'} "
          f"{'pruned' if args.apply else 'prunable'}.")
    if not args.apply and (total_files or total_prunable):
        print("Re-run with --apply to enforce.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
