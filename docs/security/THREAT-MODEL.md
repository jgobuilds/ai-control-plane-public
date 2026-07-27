# Brightworks — threat model & security findings

Red-team of the initial build (v0.1). Status keys: **FIXED** (addressed in the
hardening pass), **OPEN** (documented, not yet fixed), **ACCEPTED** (residual
risk we knowingly carry at this stage).

Trust boundaries, outermost → innermost:
1. **Host / LAN** — where n8n's UI is reachable.
2. **n8n** — orchestrator; holds notify-webhook secrets; drives every other service.
3. **router / scrubber** — policy + PII chokepoints on the internal docker network.
4. **runners** — firewalled containers running agent CLIs with
   `--dangerously-skip-permissions`.
5. **vault** — token→PII maps, mounted only into the scrubber.

The core design intent: *isolation is structural, not trusted*. The findings
below are ranked by how far each one falls short of that intent.

---

## Critical

### C1 — Pool-mode scope isolation is advisory, not structural  · OPEN
Every runner bind-mounts the **entire** `./workspace` (all scopes). The router
only sets the agent's `cwd`; it does not limit what the agent can read. Because
runners run full Claude Code with `--dangerously-skip-permissions`, a `Bash`
step (`cat /workspace/scopes/**/client-b/**`) reads any sibling scope. `cwd` is a
context/UX convenience, **not a boundary**.
- **Exploit:** an agent in a low-trust scope reads a confidential client's files.
- **Impact:** the marquee guarantee is false for pool mode; ethical walls between
  competing clients are unenforced unless dedicated silo runners exist (they
  don't — see L3).
- **Fix (deferred, deeper change):** one container per scope subtree mounting
  *only* that subtree (make `isolation:"silo"` real in compose), or a per-request
  ephemeral mount. Until then, **CONTEXT-ARCHITECTURE.md's "can't leak a directory
  it never had mounted" is only true in silo mode** — pool mode is advisory.

### C2 — The router is not the single entry point  · OPEN
`fanout`, `incident-responder`, and `dependency-bumps` call the runner's
`/fanout` and `/run` **directly**, bypassing scope validation, ethical-wall
checks, the PII prompt guard, and tier clamping. Enforcement exists only on the
`/route` path.
- **Fix (deferred):** move scope/PII enforcement *into* the runner (defence in
  depth), or force all traffic — including fan-out — through the router.

### C3 — n8n is the crown jewels and the most exposed surface  · FIXED (partial)
n8n was published on `0.0.0.0:5678` (LAN-reachable), default-no-auth, with env
access enabled in Code nodes.
- **Fixed:** bound to `127.0.0.1:5678`; README/compose now require completing
  n8n owner-account setup before use.
- **Still your job (OPEN):** enable n8n user management / SSO (or put IAP in
  front, per GSUITE-GCP.md) — a localhost bind stops the LAN, not a local user.
  `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` remains (the notify seam needs `$env`);
  the auth tokens are **not** in n8n's env (they live in workflow node params),
  so the env exposure is limited to notify-webhook URLs. Treat those as secrets.

---

## High

### H1 — One shared secret, fail-open, timing-unsafe  · FIXED
`ROUTER_TOKEN`/`SCRUBBER_TOKEN` defaulted to `RUNNER_TOKEN` (one leak = total),
auth was disabled entirely when the env var was unset (fail-open), and
comparisons were non-constant-time.
- **Fixed:** distinct required tokens per service (compose uses `:?` so it
  refuses to start unset); every service refuses to boot without its token
  unless `ALLOW_NO_AUTH=1` (local dev only); comparisons now use
  `crypto.timingSafeEqual`.

### H2 — `/rehydrate` is a PII de-tokenization oracle over HTTP  · OPEN
The vault-not-in-runners split is sound, but `/rehydrate` (and `/outbox` with
rehydration) re-expose that capability to any holder of the scrubber token — and
n8n holds it.
- **Partial mitigation (from H1):** the scrubber now has its own distinct token,
  no longer shared with the runners.
- **Fix (recommended next):** a *separate* `REHYDRATE_TOKEN` gating only the
  PII-revealing endpoints, held by the one workflow that legitimately ships
  deliverables; log every rehydrate call with scope + caller.

### H3 — Prompt injection → cross-scope exfiltration  · MITIGATED (partial; ML backend optional)
Ingested docs and incident payloads are scrubbed for structured PII but not for
injection, then fed to a skip-permissions agent. Chains with C1 (shared mount)
and the deliverables lane to exfil confidential prose to Drive; the egress PII
guard only catches structured PII, not arbitrary text.
- **Added (defense-in-depth):** heuristic `inputScan`/`outputScan`
  (`router/guardrails.js`, warn/block per `policy.json guardrails`) flags
  injection shapes and secret/exfil leakage; **per-scope `allowedTools`** least-
  privilege denies tools a scope doesn't need (see GUARDRAILS.md). Only finding
  types reach the audit ledger.
- **Still the real fix:** C1 — an agent that cannot read client-B cannot leak it.
  Heuristics catch the obvious, not the clever; treat ingested/event content as
  data, never instructions.

### H4 — Firewall fails open; DNS is an open exfil channel  · FIXED
`entrypoint.sh` ran `init-firewall.sh || echo WARNING` and continued — a failed
apply meant **full internet egress**. DNS was allowed to any resolver.
- **Fixed:** with `FIREWALL=on`, a failed apply now **aborts the container**
  (fail-closed); DNS restricted to docker's resolver `127.0.0.11`.
- **Residual (ACCEPTED):** IPs are still resolved once at boot (CDN rotation →
  possible availability failures; re-resolve or use an SNI-filtering proxy for
  production). DNS via `127.0.0.11` can still forward upstream — a slow covert
  channel, but no longer to an arbitrary attacker-chosen resolver.

---

## Medium  · all OPEN

- **M1 — Vault plaintext at rest.** `vault/<scope>.json` centralizes real PII in
  cleartext (gitignored, unencrypted). Deterministic tokens also leak *equality*
  (same token across docs = same person). *Fix:* encrypt at rest (age/KMS);
  access-log rehydrate.
- **M2 — PII detection is regex-only, US-only, no Luhn, no NER.**  · MITIGATED
  (when enabled) The heuristic misses names, addresses, DOB, non-US formats.
  **Added:** a pluggable detector seam — `SCRUB_BACKEND=presidio|dlp` delegates
  detection to an NER-grade ML service (Presidio / Cloud DLP), tokenized by the
  same deterministic vault; heuristic remains the default + fail-safe fallback
  (see DETECTION-BACKENDS.md). **Residual:** heuristic is still the floor when no
  backend is configured.
- **M3 — `fanout` `repo` param unvalidated** (`repo:"../.."` escapes the
  workspace) while `/run` `cwd` is hardened; `/run` also trusts caller `cwd`, so
  a token-holder bypasses router scope checks. *Fix:* apply `safeCwd` to `repo`.
- **M4 — Cross-provider verify doubles vendor exposure.** Confidential-scope work
  is sent to *both* Anthropic and Google. **MITIGATED:** the risk layer's
  `blockCrossVendor` (data ≥ 3) keeps the verifier on the same vendor for
  confidential scopes on the `/route` path (see RISK-MODEL.md). Residual: direct
  runner lanes (C2) until cutover.
- **M5 — Approval links are unauthenticated capability URLs.**  · FIXED (partial)
  Anyone with the resume link could approve; no approver authz and no
  segregation of duties. **Fixed (router-side authz):** the router now enforces
  approver RBAC + SoD on the `/route` path — an `approved:true` request must name
  an `approver` whose `identityGroup` is authorized to approve at that request's
  risk level (and scope, when listed), and the approver must differ from the
  `requester` at high/critical risk; otherwise `blocked:"approval-unauthorized"`
  (still audited, with requester/approver recorded). Policy in
  `router/policy.json → approvers`; logic in `router/rbac.js`; see RBAC.md.
  **Still open (identity capture):** the n8n resume URL remains an
  unauthenticated bearer capability — the RBAC check is only as strong as the
  `approver`/`approverGroups` fed to it. *Remaining fix:* authenticate the
  approval ingress (Google SSO / IAP header → verified identity + group lookup),
  or issue signed/expiring links bound to a named approver, and pass that
  identity to the router. Tracked in RBAC.md and the approval-gate `meta`.
- **M6 — `clean:true` force-deletes unmerged branches** (dependency-bumps runs
  weekly) — silent loss if a human is mid-review. *Fix:* skip/annotate branches
  with unmerged commits.
- **M7 — No CPU/mem/pids limits.** A `verify` fork-bomb takes the host. *Fix:*
  `deploy.resources` / `ulimits` per container.
- **M8 — Secrets in env** (visible to `docker inspect`, `/proc`, n8n Code nodes).
  *Fix:* docker secrets / mounted files.

## Low  · OPEN

- **L1 — No durable audit log in the router.** The `decision` block is returned
  but not persisted — a governance gap for the consulting story.
- **L2 — 500 responses echo internal paths** (info disclosure).
- **L3 — silo `runnerHost`s in `scopes.json` don't exist in compose** — silo
  scopes currently fail (but fail *closed*).

---

## What's actually solid
The vault-not-in-runners split (H2 notwithstanding) and the human gates on
irreversible acts (ship, promote, apply-fix) are the right shapes. The isolation
model is the weak leg — and it's the product — so **C1 → C2 are the deep fixes
to schedule before real client data touches this.**

## Remediation order
1. **C3** — lock n8n (done: localhost bind; you: enable owner auth). ✅ pass 1
2. **H1** — per-service tokens, fail-closed, constant-time. ✅ pass 1
3. **H4** — fail-closed firewall + DNS restriction. ✅ pass 1
4. **C1** — real per-scope mounts (silo runners). ⟵ next deep change
5. **C2** — enforcement inside the runner.
6. **H2 / M1** — rehydrate token + vault encryption.
