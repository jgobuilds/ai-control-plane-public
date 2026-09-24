# Pluggable ML detection backends (E3)

The two heuristic detection layers — PII tokenization (`scrubber`) and
injection/output guardrails (`router/guardrails.js`) — can optionally delegate to
an **ML detector** (Presidio (`data-privacy-stack/presidio`, formerly Microsoft) / Google Cloud DLP for PII; Lakera Guard /
AWS Bedrock Guardrails for injection). This closes threat-model **M2** (regex-only
PII) and **H3** (heuristic injection) *when configured*.

Three invariants:

- **Heuristic is the default and the floor.** With nothing configured the built-in
  regex path runs **byte-identical** to before — no network call, no behavior
  change. The ML backend *adds* coverage; it never replaces the floor.
- **No new dependencies.** Backends are reached over `http`/`https` (Node
  built-ins). Presidio, Cloud DLP, and Lakera all expose HTTP endpoints; run
  Presidio as a sidecar container, or point at a managed API.
- **Fail-safe.** A backend error/timeout falls back to the heuristic detector and
  flags it (`backendError` on the scrub result; a `backend-error` finding on
  guardrails). Text is **never** passed through unredacted because a detector was
  unreachable.

## PII detection — scrubber

Config (env on the `scrubber` service):

| Env | Default | Meaning |
|---|---|---|
| `SCRUB_BACKEND` | `heuristic` | `heuristic` \| `presidio` \| `dlp` |
| `SCRUB_BACKEND_URL` | `""` | detector endpoint (required for non-heuristic) |
| `SCRUB_BACKEND_TIMEOUT_MS` | `5000` | per-request timeout |

The scrubber POSTs `{ "text": "...", "language": "en" }` and normalizes the
response to spans, then tokenizes them with the **same deterministic vault** (so
`«PERSON_1»` semantics, rehydration, and the vault-never-in-runners guarantee are
unchanged). Accepted response shapes (`backendSpans`):

- **Presidio analyzer** — an array of `{ entity_type, start, end, score }`; the
  span text is `text.slice(start, end)`.
- **Generic** — `{ "entities": [ { "type": "PERSON", "value": "Jane Doe" } ] }`.

Only detection is delegated; tokenization/rehydration stay in the control plane, so the
raw values still never leave the scrubber's vault.

## Injection / output guardrails — router

Config (in `router/policy.json → guardrails`):

| Key | Default | Meaning |
|---|---|---|
| `backend` | `heuristic` | `heuristic` \| `lakera` \| `bedrock` |
| `backendUrl` | `""` | detector endpoint |

The router POSTs `{ "text": "...", "backend": "lakera" }` and merges the backend
findings with the heuristic ones (heuristic always runs first as the floor).
Accepted response shapes (`backendFindings`):

- **Generic** — `{ "findings": [ { "type": "prompt_injection", "level": "high",
  "snippet": "…" } ] }`.
- **Flag-style** (Lakera-ish) — `{ "flagged": true }` or `{ "results": [ {
  "flagged": true } ] }` → one `high` finding.

`level` and blocking still obey `policy.guardrails` (`warn`/`block`,
`minLevelToBlock`); the backend just contributes findings. Audit stays
metadata-only — finding **types** only, never the matched text.

## Standing up a backend

- **Presidio:** run `mcr.microsoft.com/presidio-analyzer` (or self-host) on the
  internal network; `SCRUB_BACKEND=presidio`, `SCRUB_BACKEND_URL=http://presidio-analyzer:3000/analyze`.
  Add its host to the scrubber's egress allowlist if it's external.
- **Cloud DLP / Lakera / Bedrock:** front the managed API with a tiny adapter that
  emits one of the shapes above (keeps provider credentials out of the control plane and
  the response contract stable). Point `*_URL` at the adapter.

## What this does and doesn't do

- **Does:** add NER-grade PII (names/addresses) and trained injection detection
  *on top of* the regex floor, per the market's "containment + detection" posture.
- **Doesn't:** make detection a guarantee (injection is unsolved at the model
  layer) — the structural controls (C1 isolation, tool allowlisting) remain the
  real boundary. Detection raises cost and produces audit signal.

Tested by `tests/detection_backend_test.py` (adapter contract + default-path-unchanged,
runnable now) and `tests/unit/detection.test.mjs` (heuristic-vs-mock-backend +
fallback, CI).
