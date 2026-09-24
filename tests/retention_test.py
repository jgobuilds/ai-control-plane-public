#!/usr/bin/env python3
"""Tests for the retention sweep + provable offboarding scripts.

Runnable NOW with no services. Builds a throwaway repo layout (context/scopes.json
+ workspace/scopes/... + vault/ + audit/) in a temp dir, backdates some files with
os.utime, then drives the real script logic (imported from scripts/) and asserts:

  retention_sweep:
    - old files (past retention) are selected; new files are kept
    - dry-run deletes nothing; --apply deletes exactly the old ones
    - vault prune is CONSERVATIVE: a token still present in a surviving file is
      never pruned; a token orphaned by the deletion is pruned only on --apply

  offboard_scope:
    - refuses a scope with children unless --recursive
    - dry-run leaves everything in place and writes no certificate
    - --confirm removes the subtree + vault and writes a certificate whose
      manifest sha256/size match the files that existed before deletion

    python tests/retention_test.py     # exits non-zero on any failure
"""
import os, sys, json, tempfile, shutil, hashlib, datetime, importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")


def _load(mod_name, filename):
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(SCRIPTS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

sweep = _load("retention_sweep", "retention_sweep.py")
offboard = _load("offboard_scope", "offboard_scope.py")

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


SCOPES = {
    "levels": ["enterprise", "team"],
    "workspaceRoot": "scopes",
    "levelDefaults": {
        "enterprise": {"retentionDays": None},
        "team": {"retentionDays": 30},
    },
    "nodes": {
        "enterprise": {"level": "enterprise", "parent": None},
        "acme": {"level": "team", "parent": "enterprise"},
    },
}


def build_repo(tmp):
    os.makedirs(os.path.join(tmp, "context"))
    with open(os.path.join(tmp, "context", "scopes.json"), "w", encoding="utf-8") as f:
        json.dump(SCOPES, f)
    for sub in ("ingest", "deliverables", os.path.join("deliverables", "sent")):
        os.makedirs(os.path.join(tmp, "workspace", "scopes", "enterprise", "acme", sub))
    os.makedirs(os.path.join(tmp, "vault"))
    os.makedirs(os.path.join(tmp, "audit"))


def write_file(tmp, relparts, text, age_days=None):
    p = os.path.join(tmp, "workspace", "scopes", "enterprise", "acme", *relparts)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    if age_days is not None:
        ts = (datetime.datetime.now() - datetime.timedelta(days=age_days)).timestamp()
        os.utime(p, (ts, ts))
    return p


def test_retention():
    print("retention_sweep:")
    tmp = tempfile.mkdtemp(prefix="ret-")
    try:
        build_repo(tmp)
        # old ingest file references «EMAIL_1»; new deliverable references «PHONE_1».
        old = write_file(tmp, ["ingest", "old.md"], "hello «EMAIL_1» bye", age_days=100)
        new = write_file(tmp, ["deliverables", "new.md"], "current «PHONE_1»", age_days=1)
        # a second old file also referencing «PHONE_1» — so PHONE_1 survives (kept) via `new`.
        write_file(tmp, ["deliverables", "sent", "old-sent.md"], "archived «PHONE_1»", age_days=200)

        vault = {
            "byValue": {"EMAIL:a@b.com": "«EMAIL_1»", "PHONE:555-111-2222": "«PHONE_1»"},
            "byToken": {"«EMAIL_1»": "a@b.com", "«PHONE_1»": "555-111-2222"},
            "counters": {"EMAIL": 1, "PHONE": 1},
        }
        with open(os.path.join(tmp, "vault", "acme.json"), "w", encoding="utf-8") as f:
            json.dump(vault, f)

        now = datetime.datetime.now()

        # --- dry run ---
        rep = sweep.sweep_scope(tmp, SCOPES, "acme", now, do_apply=False)
        deleted = {os.path.basename(r["path"]) for r in rep["to_delete"]}
        check("old files selected", "old.md" in deleted and "old-sent.md" in deleted, str(deleted))
        check("new file kept", "new.md" not in deleted, str(deleted))
        check("dry-run deletes nothing", os.path.exists(old) and os.path.exists(new))
        # EMAIL_1 only lived in old.md (to be deleted) -> orphaned -> prunable.
        # PHONE_1 lives in new.md (surviving) -> still referenced -> NOT prunable.
        check("orphaned token prunable", rep["vault_prunable"] == ["«EMAIL_1»"], str(rep["vault_prunable"]))
        check("referenced token NOT pruned", "«PHONE_1»" not in rep["vault_prunable"])
        check("dry-run leaves vault untouched",
              json.load(open(os.path.join(tmp, "vault", "acme.json"))) == vault)

        # --- apply ---
        rep2 = sweep.sweep_scope(tmp, SCOPES, "acme", now, do_apply=True)
        check("apply deletes old file", not os.path.exists(old))
        check("apply keeps new file", os.path.exists(new))
        v2 = json.load(open(os.path.join(tmp, "vault", "acme.json")))
        check("apply prunes orphaned token", "«EMAIL_1»" not in v2["byToken"])
        check("apply keeps referenced token", "«PHONE_1»" in v2["byToken"])
        check("apply prunes byValue side too", "EMAIL:a@b.com" not in v2["byValue"])
        check("apply preserves counters", v2["counters"] == {"EMAIL": 1, "PHONE": 1})

        # enterprise (retentionDays None) is skipped entirely
        check("keep-forever scope skipped", sweep.sweep_scope(tmp, SCOPES, "enterprise", now, False) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_offboard():
    print("offboard_scope:")
    tmp = tempfile.mkdtemp(prefix="off-")
    try:
        build_repo(tmp)
        f1 = write_file(tmp, ["ingest", "src.md"], "tenant material «EMAIL_1»")
        f2 = write_file(tmp, ["deliverables", "out.md"], "report body")
        vpath = os.path.join(tmp, "vault", "acme.json")
        with open(vpath, "w", encoding="utf-8") as f:
            json.dump({"byToken": {"«EMAIL_1»": "a@b.com"}}, f)

        # pre-compute expected hashes
        def sha(p):
            return hashlib.sha256(open(p, "rb").read()).hexdigest()
        expected = {
            "workspace/scopes/enterprise/acme/ingest/src.md": sha(f1),
            "workspace/scopes/enterprise/acme/deliverables/out.md": sha(f2),
            "vault/acme.json": sha(vpath),
        }

        # refuses parent-with-children without --recursive
        try:
            offboard.main(["enterprise", "--root", tmp, "--confirm"])
            check("refuses scope with children", False)
        except SystemExit as e:
            check("refuses scope with children", "child" in str(e).lower(), str(e))

        # dry-run changes nothing, writes no certificate
        rc = offboard.main(["acme", "--root", tmp, "--operator", "tester"])
        check("dry-run returns 0", rc == 0)
        check("dry-run leaves files", os.path.exists(f1) and os.path.exists(vpath))
        cert_dir = os.path.join(tmp, "audit", "deletions")
        check("dry-run writes no certificate", not os.path.isdir(cert_dir) or not os.listdir(cert_dir))

        # confirm executes
        rc = offboard.main(["acme", "--root", tmp, "--operator", "tester", "--confirm"])
        check("confirm returns 0", rc == 0)
        check("subtree removed",
              not os.path.exists(os.path.join(tmp, "workspace", "scopes", "enterprise", "acme")))
        check("vault removed", not os.path.exists(vpath))

        certs = os.listdir(cert_dir)
        check("certificate written", len(certs) == 1, str(certs))
        cert = json.load(open(os.path.join(cert_dir, certs[0])))
        check("certificate scope correct", cert["scope"] == "acme")
        check("certificate operator correct", cert["operator"] == "tester")
        check("certificate not dry-run", cert["dryRun"] is False)
        check("certificate chain correct", cert["chain"] == ["enterprise", "acme"])
        got = {m["path"]: m["sha256"] for m in cert["manifest"]}
        check("manifest covers all files", set(got) == set(expected), f"{set(got)} vs {set(expected)}")
        check("manifest sha256 all correct", got == expected)
        check("certificate filename stamped", certs[0].startswith("acme-") and certs[0].endswith(".json"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_recursive_offboard():
    print("offboard_scope (recursive):")
    tmp = tempfile.mkdtemp(prefix="offr-")
    try:
        # add a child scope 'acme-sub' under 'acme'
        scopes = json.loads(json.dumps(SCOPES))
        scopes["nodes"]["acme-sub"] = {"level": "team", "parent": "acme"}
        os.makedirs(os.path.join(tmp, "context"))
        with open(os.path.join(tmp, "context", "scopes.json"), "w") as f:
            json.dump(scopes, f)
        sub = os.path.join(tmp, "workspace", "scopes", "enterprise", "acme", "acme-sub", "ingest")
        os.makedirs(sub)
        os.makedirs(os.path.join(tmp, "vault"))
        os.makedirs(os.path.join(tmp, "audit"))
        with open(os.path.join(sub, "child.md"), "w") as f:
            f.write("child «EMAIL_9»")
        with open(os.path.join(tmp, "vault", "acme.json"), "w") as f:
            json.dump({"byToken": {}}, f)
        with open(os.path.join(tmp, "vault", "acme-sub.json"), "w") as f:
            json.dump({"byToken": {"«EMAIL_9»": "c@d.com"}}, f)

        rc = offboard.main(["acme", "--root", tmp, "--recursive", "--confirm", "--operator", "op"])
        check("recursive confirm returns 0", rc == 0)
        check("child subtree gone",
              not os.path.exists(os.path.join(tmp, "workspace", "scopes", "enterprise", "acme")))
        check("child vault gone", not os.path.exists(os.path.join(tmp, "vault", "acme-sub.json")))
        cert = json.load(open(os.path.join(tmp, "audit", "deletions",
                          os.listdir(os.path.join(tmp, "audit", "deletions"))[0])))
        check("cert lists both scopes purged", set(cert["scopesPurged"]) == {"acme", "acme-sub"},
              str(cert["scopesPurged"]))
        check("cert manifest includes child vault",
              "vault/acme-sub.json" in {m["path"] for m in cert["manifest"]})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_retention()
    test_offboard()
    test_recursive_offboard()
    print()
    if fails:
        print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
        sys.exit(1)
    print("All retention/offboarding tests passed.")
