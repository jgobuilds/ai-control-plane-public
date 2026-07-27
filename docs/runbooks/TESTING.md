# Testing — how to prove Brightworks is secure and works as designed

The governance rules ARE the product, so they carry a layered test suite: fast
Python checks that run anywhere (no Node, no Docker), CI-runnable Node unit tests
over the servers' pure functions, and one live smoke test against the Docker
stack. Everything below is green in the dev sandbox except the two clearly-marked
Node/Docker layers.

## Run it

```bash
# 1. Governance conformance (source-of-truth invariants) — must be green
python scripts/conformance_test.py

# 2. Per-feature Python tests (security, audit, PII, RBAC, kill switch, …)
python tests/security_test.py
python tests/audit_chain_test.py
python tests/scrub_test.py
python tests/guardrails_test.py
python tests/killswitch_test.py
python tests/rbac_policy_test.py
python tests/retention_test.py
python tests/usecase_test.py

# 3. Audit ledger verifier (tamper-evidence) — standalone
python scripts/verify_audit.py            # walks ./audit/decisions.jsonl

# 4. Node unit tests over the servers' pure functions — CI-runnable (Linux CI).
#    NOT run in the node-free dev sandbox; wired for `node --test` in CI.
node --test tests/unit/

# 5. Live functional smoke test — needs the Docker stack up. FAILS LOUDLY.
#    Requires ALLOW_NO_AUTH=1 (dev) or a real ROUTER_TOKEN. See the header.
ROUTER_TOKEN=xxx bash scripts/smoke_test.sh
```

### One-liner (everything that runs without Node/Docker)

```bash
python scripts/conformance_test.py && \
for t in tests/*_test.py; do python "$t" || exit 1; done && \
python scripts/verify_audit.py
```

## Making the servers unit-testable

Each `server.js` (`router`, `scrubber`, `claude-runner`, `gemini-runner`) guards
its `server.listen(...)` behind `if (require.main === module)` and adds a
`module.exports` of its pure helpers. Importing a server in a test therefore
never binds a port and never changes runtime behavior — the container entrypoint
(`node /app/server.js`) still runs `main`, so it listens exactly as before. The
scrubber additionally reads `VAULT_DIR`/`WORKSPACE_DIR` from env (defaulting to
the container mounts `/vault` and `/workspace`) so a test can redirect the token
vault to a temp dir; in the image those vars are unset, so behavior is identical.

The Node tests set the servers' existing env knobs (`POLICY_PATH`, `RULES_PATH`,
`SCOPES_PATH`, `AUDIT_DIR`, `ALLOW_NO_AUTH=1`) before a dynamic `import()`, so
they run hermetically against the repo's real policy/scope artifacts.

## What each test proves

| Test | Layer | Proves (security invariant / functional behavior) |
|---|---|---|
| `scripts/conformance_test.py` | policy/config | Ethical walls hold; every confidential scope gets a dedicated runner; **no runner mounts a foreign scope** (C1 structural proof); mode caps are monotonic; risk controls never weaken as a dimension rises; PII guard configured. |
| `scripts/verify_audit.py` | audit | The hash-chained ledger re-walks intact; any edit/reorder/delete breaks the chain (tamper-evidence). |
| `tests/audit_chain_test.py` | audit (E2E) | Builds a ledger in Python in the router's exact `<hash> <json>` format, verifies green, then edits / reorders / deletes a record and asserts `verify_audit.py` exits non-zero. Cross-language tamper-evidence, end to end. |
| `tests/security_test.py` | deploy posture | Firewalls `bash -n`-clean with **default-DROP** and DNS pinned to `127.0.0.11`; entrypoints fail **closed** on firewall failure; every server refuses to boot without its token (constant-time compare); **no runner publishes a host port**; n8n bound to `127.0.0.1`; tokens use the required `${VAR:?}` form; the vault mounts into the scrubber and **no** runner; `.gitignore` excludes secrets/PII/audit/control state. |
| `tests/scrub_test.py` | PII (mirror) | Deterministic, equality-preserving tokenization (same value → same token across calls); distinct values → distinct tokens; per-type findings counts; rehydrate round-trip; unknown-token passthrough. Mirrors `scrubber/server.js` regexes. |
| `tests/guardrails_test.py` | injection (mirror) | Known injection/exfil prompts and leaked-secret outputs match; benign text stays clean. Mirrors `router/guardrails.js`. |
| `tests/killswitch_test.py` | kill switch | Global halt stops everything; scope halt stops that scope; an ancestor halt stops descendants (chain match). |
| `tests/rbac_policy_test.py` | approver RBAC | Policy-level approver authorization + segregation-of-duties shape. |
| `tests/retention_test.py` | retention | Effective `retentionDays` ages out ingest/deliverables; offboarding deletes + certifies. |
| `tests/usecase_test.py` | conformity register | The generated use-case register can't drift from the enforced artifacts. |
| `tests/unit/router.test.mjs` | router (Node) | `tokenOk` (constant-time, empty ⇒ fail-closed unless `ALLOW_NO_AUTH`); `classifyRisk` matrix (silo·advise→high, warn·write→medium, apply·proactive→critical, blockCrossVendor at data ≥ 3); `resolveMode` (scope floor, request can only tighten, human-only caps advise); `clampTier`; the audit hash-chain matches `verify_audit.py`. |
| `tests/unit/scrubber.test.mjs` | scrubber (Node) | `scrub`/`rehydrate` over the REAL module (deterministic tokens, findings, unknown-token passthrough); `resolveScope`; `ingest` silo-refusal. |
| `tests/unit/runner.test.mjs` | runners (Node) | `safeCwd` rejects `../` / absolute escapes and accepts in-workspace paths; `sanitizeAllowedTools` rejects shell metachars / leading `-`, accepts tool tokens. |
| `tests/unit/{rbac,killswitch,guardrails}.test.mjs` | modules (Node) | Full `node:test` suites over the pure policy modules. |
| `scripts/smoke_test.sh` | live E2E (Docker) | Router `/health` 200; a low-risk `/route` returns a deterministic decision; a raw email in a `pii:block` scope is refused (never forwarded); an audit record is appended and `verify_audit.py` passes. |
