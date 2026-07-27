# Runbook — wiring Google Drive OAuth for `drive-sync` and `deliverables`

Two lanes need Drive: `drive-sync` (Drive → scrubber → workspace) and
`deliverables` (approved artifacts → Drive). Both authenticate through n8n's
`googleDriveOAuth2Api` predefined credential. Neither runs until this is done —
they refuse rather than query a bogus folder, which is why an unwired stack looks
quiet rather than broken.

**Steps 1–4 must be done by a human in a browser.** They involve an OAuth client
secret and a Google consent grant; an agent should not perform either.

---

## 0. Preconditions

```bash
docker ps --format "{{.Names}}"        # n8n and scrubber must be up
```

Confirm nothing is wired yet — expect "no credentials exported":

```bash
docker exec n8n sh -c 'n8n export:credentials --all --output=/tmp/c.json >/dev/null 2>&1 \
  && node -e "console.log(require(\"/tmp/c.json\").length + \" credentials\")" || echo "none"; rm -f /tmp/c.json'
```

## 1. Google Cloud — enable the API

Console → select or create a project → **APIs & Services → Library** → *Google
Drive API* → **Enable**.

## 2. OAuth consent screen

**APIs & Services → OAuth consent screen.**

- **Internal** if this is a Google Workspace domain — no verification, no test-user
  list, and it stays inside the org. Prefer this.
- **External** otherwise — then add your own Google account under **Test users**,
  or the consent in step 4 fails with `access_denied` even though everything else
  is correct.

Scopes: leave empty here. n8n requests what it needs at connect time.

## 3. OAuth client ID

**Credentials → Create credentials → OAuth client ID → Web application.**

Under **Authorized redirect URIs** add exactly:

```
http://localhost:5678/rest/oauth2-credential/callback
```

> This is n8n's default callback, correct while no `WEBHOOK_URL` or
> `N8N_EDITOR_BASE_URL` is set in `.env`. **If you ever set either, this URI must
> change to match** — a mismatch here is the most common failure in this setup,
> and Google's error (`redirect_uri_mismatch`) names it plainly.

You get a **client ID** and a **client secret**. Treat the secret like any other:
it goes into n8n and nowhere else — not into `.env`, not into a commit, not into
a chat window.

## 4. Create the credential in n8n

`http://localhost:5678` → **Credentials → New → Google Drive OAuth2 API**.

1. Paste the client ID and client secret.
2. **Connect my account** → sign in → grant.
3. Save. The credential must be named so the two workflows resolve it; if they
   were imported before the credential existed, reopen each Drive node once and
   select it.

Verify it stored:

```bash
docker exec n8n sh -c 'n8n export:credentials --all --output=/tmp/c.json >/dev/null 2>&1 \
  && node -e "require(\"/tmp/c.json\").forEach(c=>console.log(c.name,\"|\",c.type))"; rm -f /tmp/c.json'
```

Never run `export:credentials --decrypted`. It writes secrets to disk in plain
text, and there is no reason to.

## 5. Point the lanes at a folder

From the Drive URL `https://drive.google.com/drive/folders/<ID>`, take `<ID>`
into `.env`:

```
DRIVE_FOLDER_ID=<the id>
```

Not a secret — but note the lanes read it at **container start**, so:

```bash
docker compose up -d --force-recreate lanes n8n
```

A plain restart does not re-read `.env`.

## 6. Activate

```bash
python scripts/n8n_bootstrap.py --activate brightworks-drive-sync
python scripts/n8n_bootstrap.py --activate brightworks-deliverables
docker exec n8n n8n list:workflow --active=true
```

## 7. Verify before trusting it

`--dry-run` **cannot be used while the stack is up** — `n8n execute` spawns a
second instance and collides on task-broker port 5679. So verify the round trip
by hand:

1. Drop a small text file into the Drive folder.
2. Wait one cycle (15 min) or trigger the workflow's *Run on demand* node in the UI.
3. Confirm it landed **scrubbed**, not raw:

```bash
docker exec lanes sh -c 'ls -la /app/workspace/scopes/*/ 2>/dev/null | head'
```

The content must show substituted tokens (`«EMAIL_001»`-shaped), not the original
values. **If raw PII reaches the workspace, stop and fix the scrubber path before
going further** — that is the control this lane exists to enforce, and a sync that
bypasses it is worse than no sync.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `redirect_uri_mismatch` | Step 3 URI differs from n8n's callback, usually because `WEBHOOK_URL` got set later |
| `access_denied` on consent | External consent screen without your account in **Test users** |
| Lane runs, syncs nothing | `DRIVE_FOLDER_ID` empty, or containers not recreated after editing `.env` |
| Node shows "credential not set" | Workflow imported before the credential existed — reopen the node and select it |
| Token stops working after ~7 days | External consent screen still in **Testing**; refresh tokens expire. Publish it, or move to Internal |
| Nothing fires on schedule | Docker was suspended and swallowed the cron — check before suspecting the lane |

## What this unlocks

With Drive wired, `drive-sync` becomes the supported route for the enablement kit
to reach scope — the decision recorded in
[`docs/plans/RESEARCH-AND-MARKETING-LANES.md`](../plans/RESEARCH-AND-MARKETING-LANES.md)
§4, chosen over a read-only mount so this engine keeps no filesystem path into a
proprietary sibling repo. That is the precondition for the `content-draft` and
`practice-scout` lanes.
