# Giving the stack GCP / Gemini access

## First, the rule that governs all of it

**Never paste a credential into a chat, a commit message, an issue, or a file
that is not gitignored.** Not to an agent, not to me, not into a "temporary"
note.

An agent does not need to *see* a secret to use one. You place it where the
process reads it; the agent verifies by **behaviour** — did the runner
authenticate? — and never by reading the value. If anyone asks you to paste a
key so they can "check it", that is the wrong shape regardless of who is asking.

This is not caution for its own sake. `pii_scan` runs on staged content **and on
the commit message**, because a message is history too and history cannot be
taken back. A key pasted into a conversation is already outside the boundary this
whole system exists to hold.

---

## Which credential you actually want

The stack has **three separate Google relationships**, and today two of them are
wrong or absent. Decide each deliberately; they carry different terms.

| Path | Used by | Today | Terms |
|---|---|---|---|
| **AI Studio API key** | `gemini-runner` | **in use** — `GEMINI_API_KEY` is set | consumer free tier |
| **Workspace OAuth** | Drive, Meet transcription | not configured | Workspace DPA |
| **GCP service account / ADC** | Vertex, DLP, Secret Manager | not configured | GCP DPA |

**The live gap:** `gemini-runner` authenticates with an AI Studio free-tier key,
while `data-handling.md` records "Google" as an approved processor on the
strength of a *Workspace* relationship. Same vendor, different terms, and every
document reads as compliant. Verified: `GEMINI_API_KEY` is set in the container
and `.gemini/projects.json` is `{"projects": {}}` — no Workspace login was ever
used. See ADR 0015.

---

## A. Move the Gemini runner off the free tier

Two options. Pick on terms, not convenience.

**A1 — Workspace/Google login (no key).** `docker-compose.yml` already supports
this: *"Leave blank to fall back to a mounted Google login in `gemini-config`."*

```bash
# 1. Clear the free-tier key in .env  (gitignored — never commit it)
#    GEMINI_API_KEY=
# 2. Log in inside the runner, once. The volume persists it.
docker exec -it gemini-runner gemini
# 3. Confirm it took — by behaviour, not by reading anything:
docker exec gemini-runner sh -c 'cat /home/node/.gemini/projects.json'
#    Non-empty means the login landed.
```

**A2 — Vertex AI on a GCP project** (enterprise terms, metered billing):

1. Create or choose a GCP project **inside your Workspace organization** — a
   project outside it does not inherit the org's agreements.
2. Enable the Vertex AI API.
3. Create a service account with **only** `roles/aiplatform.user`. Not Editor,
   not Owner.
4. Download the JSON key **to disk**, never through a chat.
5. Mount it read-only and point the CLI at it — add to `docker-compose.yml`:

```yaml
      - ./secrets/vertex-sa.json:/secrets/vertex-sa.json:ro
    environment:
      - GOOGLE_APPLICATION_CREDENTIALS=/secrets/vertex-sa.json
      - GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT:?set it in .env}
```

6. **Add `secrets/` to `.gitignore` before creating the file**, not after. A
   secret that reaches the index once is in the reflog even after you remove it.

> `${VAR:?}` rather than `${VAR:-}` is deliberate and enforced by
> `security_test.py`: the `:-` form defaults to empty and turns every call into a
> silent 401. The `:?` form refuses to start, which is the failure you want.

## B. Workspace access for Drive and Meet transcripts

`drive-sync` needs an OAuth2 credential attached **in n8n**, not in `.env`:

1. GCP console → APIs & Services → **Enable** the Google Drive API.
2. **Create OAuth client ID** (Web application). Redirect URI:
   `http://localhost:5678/rest/oauth2-credential/callback`
3. In n8n → Credentials → **Google Drive OAuth2 API** → paste client ID/secret
   **into n8n**, and complete the consent flow in your browser.
4. Attach that credential to the two HTTP nodes in `drive-sync`, set `FOLDER_ID`
   and the scope in its Config node, then activate it.

n8n encrypts credentials with the key in the `n8n_data` volume. That is why ADR
0009 says to **migrate that volume, never recreate it** — recreating it silently
invalidates every stored credential.

> **`drive-sync` has never run.** It is listed as "needs OAuth". Treat its first
> run as untested rather than as a working ingest path.

## C. What to tell me

Say what you did, never what the value is:

- *"AI Studio key cleared; Workspace login done in the runner."*
- *"Vertex SA mounted at `/secrets/vertex-sa.json`, project set in `.env`."*
- *"Drive OAuth attached in n8n; `FOLDER_ID` set to the engagement folder."*

Then I verify by behaviour — `projects.json` non-empty, a `/run` that returns,
`drive-sync` landing a tokenized file in the scope's `ingest/` — and record the
finding. **If I ever ask you to paste a key, refuse.**

## D. Before any of this touches tenant data

Not optional, and not in this order by accident:

1. **Consent** is settled in the engagement contract (ADR 0015). The tenant's
   jurisdiction binds, not yours.
2. **Retention is real.** The sweep is **dry-run only** today — no deletions. A
   recording held under a sweep that deletes nothing is worse than not holding
   it.
3. **The scrubber is proven on real input.** The vault exists and has never
   carried live data; the first tenant transcript is not the run to discover a
   defect on.
4. **`cloud-providers.json` records which provider you chose**, and
   `scripts/cloud.py --egress` shows exactly which domains that opens. Runners
   are default-deny; opening one is a decision, not a consequence.
