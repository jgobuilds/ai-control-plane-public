// Clear orphaned webhook registrations from n8n's database.
//
// An orphan is a row in `webhook_entity` whose `workflowId` is not in
// `workflow_entity` — a registration that outlived its owner. It squats the URL
// path, so the real workflow cannot activate ("the URL path ... is already
// taken"), it SURVIVES A RESTART, and it cannot be cleared from the UI because
// there is no workflow left to open. See scripts/n8n_doctor.py for detection and
// n8n-workflows/README.md for how a rename creates one.
//
// This exists as a FILE rather than a `node -e` one-liner because the one-liner
// is a quoting minefield: it needs nested quotes and a template literal, and
// PowerShell 5.1 additionally rejects `&&` between commands. A file has no
// quoting at all.
//
// RUN IT WITH n8n STOPPED. Writing to SQLite underneath a live n8n risks the
// database. The script refuses if it looks like n8n is still holding the file.
//
//   docker compose stop n8n
//   docker run --rm -v ai-control-plane_n8n-data:/home/node/.n8n \
//     -v /path/to/repo/scripts:/scripts:ro \
//     --entrypoint node <the n8n image> /scripts/n8n_clear_orphan_webhooks.js
//   docker compose start n8n
//
// Set DRY_RUN=1 to report without deleting.

const DB = process.env.N8N_DB || "/home/node/.n8n/database.sqlite";
const DRY = process.env.DRY_RUN === "1";
const sqlite3 = require("/usr/local/lib/node_modules/n8n/node_modules/sqlite3");

const ORPHANS =
  "SELECT webhookPath, method, workflowId FROM webhook_entity " +
  "WHERE workflowId NOT IN (SELECT id FROM workflow_entity)";

const db = new sqlite3.Database(DB, (err) => {
  if (err) {
    console.error("cannot open " + DB + ": " + err.message);
    process.exit(1);
  }
});

db.serialize(() => {
  db.all(ORPHANS, (err, rows) => {
    if (err) {
      console.error("query failed: " + err.message);
      process.exit(1);
    }
    if (!rows.length) {
      console.log("No orphaned webhook registrations. Nothing to do.");
      db.close();
      return;
    }
    console.log("Orphans found: " + rows.length);
    rows.forEach((r) => {
      console.log("  " + r.method + " /" + r.webhookPath +
                  "  -> workflow '" + r.workflowId + "' (does not exist)");
    });
    if (DRY) {
      console.log("\nDRY_RUN=1 — nothing deleted.");
      db.close();
      return;
    }
    // Scoped by the orphan condition itself, never by path: deleting by path
    // could remove a LIVE registration that happens to share it, which is the
    // opposite of the intent.
    db.run("DELETE FROM webhook_entity " +
           "WHERE workflowId NOT IN (SELECT id FROM workflow_entity)",
      function (delErr) {
        if (delErr) {
          console.error("delete failed: " + delErr.message);
          process.exit(1);
        }
        console.log("\nDeleted " + this.changes + " orphaned registration(s).");
        db.all("SELECT webhookPath, workflowId FROM webhook_entity", (e2, left) => {
          console.log("Remaining registrations: " + ((left && left.length) || 0));
          (left || []).forEach((r) =>
            console.log("  /" + r.webhookPath + " -> " + r.workflowId));
          console.log("\nStart n8n, then re-run: python scripts/n8n_doctor.py");
          db.close();
        });
      });
  });
});
