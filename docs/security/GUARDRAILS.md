# Guardrails — injection/output scanning + per-scope tool allowlisting

Two defense-in-depth controls for threat-model **H3** (prompt injection →
cross-scope exfiltration) and the **excessive-agency** theme. Neither is a
guarantee — the structural mitigation is still C1 (per-scope mounts) + least
privilege. These raise the cost of an attack and produce audit signal.

## 1. Prompt-injection & output guardrails (heuristic)

`router/guardrails.js` — pure, dependency-free, tunable pattern arrays.

- **`inputScan(prompt)`** flags well-known injection shapes: instruction-override
  ("ignore previous instructions"), role reassignment ("you are now…"), jailbreak
  markers, exfiltration verbs aimed at secrets, path traversal (`../`),
  cross-scope path references (`/workspace/scopes/<other>`), and large opaque
  base64/hex blobs. Each finding has a severity `level` (low|medium|high).
- **`outputScan(text)`** flags secret/exfil patterns in runner output: private-key
  headers, AWS/Google/GitHub/Slack/OpenAI keys, bearer tokens, JWTs, cross-scope
  filenames.

**Policy** (`policy.json → guardrails`):
```jsonc
{ "input": "warn",          // off | warn (attach findings) | block
  "output": "warn",         // off | warn (attach outputFindings)
  "minLevelToBlock": "medium" }  // lowest severity that blocks under input:"block"
```

**Enforcement** (router `route()`, step 0a, before dispatch): `inputScan` runs on
the prompt. Under `block`, a finding at/above `minLevelToBlock` returns
`blocked:"guardrail-input"` (audited). Under `warn`, findings attach to
`decision.guardrails` and the request proceeds. When a result is available (e.g.
the verify path), `outputScan` attaches `decision.outputFindings`.

**Audit rule respected:** only finding **types** (and counts) reach the ledger —
never the matched `snippet` (which can contain prompt text). Snippets are returned
in the HTTP response for operator triage only.

**Workflow-side output scan (egress):** before a deliverable is shipped to Drive,
call `outputScan` on the rehydrated text (or add a router endpoint) and gate on
findings. Blob heuristics are deliberately high-threshold + `low` severity so they
warn rather than block by default. Tune thresholds (`BASE64_MIN`, `HEX_MIN`) and
patterns in `guardrails.js`.

## 2. Per-scope tool allowlisting (least privilege)

A scope declares which Claude Code tools its agent may use:

```jsonc
// scopes.json node
"client-a": { "controls": { "allowedTools": ["Read", "Grep", "Edit", "Bash"] } }
```

The router passes the resolved scope's `allowedTools` to the runner on dispatch;
`claude-runner` validates it (array of tool-name tokens; rejects shell
metacharacters and leading `-` to prevent flag injection — `sanitizeAllowedTools`)
and appends `--allowedTools <comma-list>` to the `claude` invocation. **Unlisted
tools are effectively denied.** Omit `allowedTools` to use the runner's defaults.

> **Verify-on-live:** the exact CLI flag is assumed to be `--allowedTools`
> (comma-separated). If your Claude Code build expects `--allowed-tools`, change
> the one line in `claude-runner/server.js`. `execFile` passes args as an array
> (no shell), so tokens are never shell-interpreted; the sanitizer is
> belt-and-suspenders.

This is defense-in-depth **on top of** the firewall (egress) and mounts (C1) — a
scope that only needs `Read`/`Grep` shouldn't be able to run `Bash` at all.

## Limits (honest)

- Injection detection is undecidable in general; `inputScan` catches the obvious,
  not the clever. Never rely on it alone.
- Enforced on the `/route` path; direct runner lanes (C2) get it at the in-runner
  PEP cutover — though tool allowlisting already flows to the runner via the
  dispatch payload and applies wherever `allowedTools` is passed.
- Tests: `tests/unit/guardrails.test.mjs` (node:test, CI) and
  `tests/guardrails_test.py` (mirrors the patterns, runnable now).
