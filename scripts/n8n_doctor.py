#!/usr/bin/env python3
"""Detect n8n live-state faults that no repo check can see.

WHY THIS EXISTS. `workflow_lint.py` checks the workflow JSON in this repo. It
cannot see n8n's own database, where the state that actually decides whether a
workflow runs lives — and that is where the failure happened:

    Workflow failed: Ask a human (HTTP door for the runner)
    The URL path that the "POST /ask-human" node uses is already taken.

The repo was clean. Exactly one workflow declared `ask-human`, and it was the
right one. The path was held by an ORPHANED REGISTRATION in `webhook_entity`
pointing at a workflow ID that no longer exists — left behind when the workflow
IDs were renamed. n8n never deregistered the old workflow's webhook, so the row
outlived its owner and kept squatting the path.

The impact is worth stating plainly, because the alert did not: the ask-human
door was returning **404**. That is the seam an agent uses to ask a human
mid-run, so every run that needed a human silently had no way to reach one.

CHECKS

  orphan-webhook   a row in webhook_entity whose workflowId is not in
                   workflow_entity. Squats the path and blocks the real owner.
  path-collision   two workflows declaring the same method+path.
  repo-drift       a webhook path in n8n-workflows/*.json that no active
                   workflow in n8n registers (imported? activated?).

Read-only. It reports and never writes: the remedy touches live orchestration
state, so it prints the command rather than running it.

    python scripts/n8n_doctor.py            # report; exit 1 on any fault
    python scripts/n8n_doctor.py --json

Needs docker and a running n8n container; SKIPs cleanly without them, so it is
safe to call from a workstation preflight and pointless in CI.
"""
import argparse, json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTAINER = os.environ.get("N8N_CONTAINER", "n8n")
DB = "/home/node/.n8n/database.sqlite"
SQLITE = "/usr/local/lib/node_modules/n8n/node_modules/sqlite3"

# One round trip, read-only. Reading n8n's SQLite while n8n runs is safe; WRITING
# under a live n8n is not, which is why the remedy is printed rather than run.
QUERY = """
const sqlite3 = require("%s");
const db = new sqlite3.Database("%s", sqlite3.OPEN_READONLY);
const out = {};
db.serialize(() => {
  db.all("SELECT webhookPath, method, workflowId, node FROM webhook_entity", (e, r) => {
    out.hooks = e ? [] : r; });
  db.all("SELECT id, active, name FROM workflow_entity", (e, r) => {
    out.workflows = e ? [] : r;
    console.log(JSON.stringify(out));
    db.close();
  });
});
""" % (SQLITE, DB)


def read_state():
    try:
        r = subprocess.run(["docker", "exec", CONTAINER, "node", "-e", QUERY],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        return None, f"docker unavailable ({e})"
    if r.returncode != 0:
        return None, f"could not read n8n state (is the container running?): {r.stderr.strip()[:200]}"
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), None
    except (ValueError, IndexError):
        return None, f"unparseable response: {r.stdout[:200]}"


def repo_paths():
    import glob
    out = {}
    for f in glob.glob(os.path.join(ROOT, "n8n-workflows", "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for n in d.get("nodes", []):
            if "webhook" in str(n.get("type", "")).lower():
                p = (n.get("parameters") or {}).get("path")
                if p:
                    out[p] = (os.path.basename(f), d.get("id"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check n8n live state for faults the repo cannot see.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    state, err = read_state()
    if err:
        print(f"SKIP  {err}")
        return 0

    ids = {w["id"] for w in state["workflows"]}
    by_id = {w["id"]: w for w in state["workflows"]}
    faults = []

    print("Orphaned webhook registrations (squat a path, block the real owner):")
    orphans = [h for h in state["hooks"] if h["workflowId"] not in ids]
    for h in orphans:
        print(f"  FAULT  {h['method']} /{h['webhookPath']}  -> workflow "
              f"{h['workflowId']!r} does not exist")
        faults.append(("orphan-webhook", h["webhookPath"], h["workflowId"]))
    if not orphans:
        print("  ok — every registration has a live owner")

    print("\nPath collisions (two workflows claiming one path):")
    seen, dupes = {}, []
    for h in state["hooks"]:
        key = (h["method"], h["webhookPath"])
        if key in seen:
            dupes.append((key, seen[key], h["workflowId"]))
        seen[key] = h["workflowId"]
    for (m, p), a, b in dupes:
        print(f"  FAULT  {m} /{p} claimed by both {a} and {b}")
        faults.append(("path-collision", p, f"{a},{b}"))
    if not dupes:
        print("  ok — no path claimed twice")

    print("\nRepo webhooks that n8n does not serve:")
    live = {h["webhookPath"] for h in state["hooks"] if h["workflowId"] in ids}
    missing = []
    for path, (fn, wid) in sorted(repo_paths().items()):
        if path in live:
            continue
        wf = by_id.get(wid)
        why = ("not imported" if wf is None
               else "imported but INACTIVE" if not wf.get("active")
               else "active but not registered — the path is probably squatted")
        print(f"  warn   /{path}  ({fn}) — {why}")
        missing.append((path, fn, why))

    if args.json:
        print(json.dumps({"faults": faults, "unserved": missing}, indent=2))

    print()
    if faults:
        print(f"FAULTS: {len(faults)}")
        if orphans:
            print()
            print("  An orphan cannot be cleared from the n8n UI — its owning workflow is")
            print("  gone, so there is nothing to open. It survives a restart. Clearing it")
            print("  writes to n8n's database, so n8n must be STOPPED first; writing")
            print("  underneath a live n8n risks the file, and this tool will not do it.")
            print()
            print("    docker compose stop n8n")
            print("    docker run --rm -v ai-control-plane_n8n-data:/home/node/.n8n \\")
            print("      -v %s/scripts:/scripts:ro \\" % ROOT.replace(os.sep, "/"))
            print("      --entrypoint node $(docker inspect n8n --format '{{.Config.Image}}') \\")
            print("      /scripts/n8n_clear_orphan_webhooks.js")
            print("    docker compose start n8n")
            print()
            print("  Set DRY_RUN=1 on the middle command to report without deleting.")
            print("  Back up /home/node/.n8n/database.sqlite (plus -wal and -shm) first.")
            print("  PowerShell 5.1: run the three separately — `&&` is not a valid")
            print("  separator there, which is also why the remedy is a script file")
            print("  rather than an inline one-liner full of nested quotes.")
        return 1
    print("No n8n live-state faults.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
