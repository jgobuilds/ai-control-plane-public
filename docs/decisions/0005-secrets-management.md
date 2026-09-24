# Secrets management: local sync now, a secrets manager as we scale

**Date:** 2026-07-23 · **Lens:** `ai-standards/references/change-safety.md`,
`ai-standards/references/build-vs-adopt.md`

## Considered

A shared secret (the Slack notify webhook) has to exist in **two** places: the
fleet's GitHub Actions secrets (for the CI failure-alert job) and
`ai-control-plane/.env` (for the n8n digest lane). The question was how to keep those
copies from drifting.

| Option | Cost | Verdict |
| --- | --- | --- |
| **Pull the value from the GitHub secret** into n8n at runtime | n/a — impossible | **Impossible** — GitHub Actions secrets are write-only; there is no API to read a secret's value back. It is only decrypted into the ephemeral runner at workflow time. |
| Push from GitHub → n8n (an Action writes the value into n8n) | n/a — impractical | Reject — n8n is localhost-bound in the WSL VM, unreachable from GitHub-hosted runners; env changes need a container recreate. Impractical. |
| **A one-input sync script** (`sync_notify_webhook.py`): edit one source, fan the value out to every repo's GitHub secret (and the `.env`) | $0 — stdlib + the `gh` CLI | **Adopt now** — cheap house glue, removes the hand-edit/drift risk for one secret. |
| **A dedicated secrets manager** both CI and n8n read at runtime (GCP Secret Manager, Vault, Doppler, Infisical, 1Password) | **metered** — per secret-version stored + per access op; small at this scale but non-zero, and **needs a billing account** | **Defer** — the correct end state; not yet worth the setup for a single secret. |

## Chose

1. **Now:** `ai-standards/scripts/sync_notify_webhook.py` — one authoritative
   entry (a `.env`, or a `--value` for rotation), fanned out to every repo's
   GitHub secret with `gh secret set`. Never prints the value (sha256-prefix
   fingerprint only); pipes the secret on stdin so it never lands in argv.
2. **Later:** adopt a real secrets manager that BOTH CI and n8n read at runtime,
   eliminating the duplication rather than syncing it.

## Because

The duplication is structural (GitHub secrets cannot be read back), so with one
shared secret the pragmatic fix is to make entering/rotating it a single command
instead of standing up infrastructure — build-vs-adopt cuts toward the script at
this size. A secrets manager earns its keep once the *number* of shared secrets
grows or a secret must reach more than one machine, because then the sync script's
"fan out to N sinks" becomes its own drift surface and the write-only-argv/`.env`
handling stops being good enough.

**GCP Secret Manager is the leading candidate** — GCP is already in the ai-control-plane
orbit (`GSUITE-GCP.md`); GitHub Actions reads it via
`google-github-actions/get-secretmanager-secrets`, and n8n reads it via
`gcloud secrets versions access` at container start. Vault/Doppler/Infisical are
alternatives if the stack moves off GCP.

## Status — the trigger to revisit

Local sync is implemented and verified (synced to all four repos; timestamps
confirmed). **Revisit and adopt a secrets manager when any of these is true:**

- more than ~3 shared secrets are being synced this way, **or**
- a secret must reach a second machine / a non-local deploy (the `.env` stops
  being one file on one host), **or**
- a secret needs rotation on a schedule or audit logging of access.

Until then, `sync_notify_webhook.py` is the sanctioned path; do not hand-edit the
webhook in individual repos.
