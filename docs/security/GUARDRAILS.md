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
"tenant-a": { "controls": { "allowedTools": ["Read", "Grep", "Edit", "Bash"] } }
```

The router passes the resolved scope's `allowedTools` to the runner on dispatch;
`claude-runner` validates it (`sanitizeAllowedTools`: tool-name tokens only, no
shell metacharacters, no leading `-`) and turns it into the flags that restrict
(`toolArgs`):

- built-in tools go to **`--tools`**, which sets what exists in the session;
- every MCP server in `.mcp.json` the list does not name as `mcp__<server>` goes
  to **`--disallowedTools`**, because `--tools` does not reach MCP tools.

Omit `allowedTools` to use the runner's defaults. An **empty** list means no
tools. The runner refuses (422) what it cannot enforce: permission specifiers
such as `Bash(git log:*)`, and tool-level MCP names (name the server instead).
`gemini-runner` can only run a session read-only: it refuses any list containing
a non-read tool, and refuses a read-only list too unless `READONLY_ARGS` is
configured.

> **This control restricted nothing until 2026-09-17 (threat-model H5).** It
> used to append `--allowedTools`, and this page said "unlisted tools are
> effectively denied" under a *verify-on-live* note that was never verified. That
> flag adds permission ALLOW rules, and the runners run with
> `--dangerously-skip-permissions`, which bypasses every permission check. A live
> probe on claude 2.1.220 ran Bash with `--allowedTools Read`. With `--tools Read`
> the session had only Read, but it still had every `mcp__ask-human__*` and
> `mcp__n8n__*` tool, until `--disallowedTools mcp__ask-human` removed that
> server. The live verification was the whole control. Regression:
> `tests/unit/runner.test.mjs` (behaviour) and `tests/security_test.py` (flags).

This is defense-in-depth **on top of** the firewall (egress) and mounts (C1) — a
scope that only needs `Read`/`Grep` shouldn't be able to run `Bash` at all.

## Limits (honest)

- Injection detection is undecidable in general; `inputScan` catches the obvious,
  not the clever. Never rely on it alone.
- Enforced on the `/route` path; direct runner lanes (C2) get it at the in-runner
  PEP cutover — though tool allowlisting already flows to the runner via the
  dispatch payload and applies wherever `allowedTools` is passed.
- A scope's list only binds if the scope declares one, and none of the sample
  scopes except `tenant-a` do. Binding a request's tools to its declared
  `action` (so a request declared as `advise` physically cannot write) is the router
  half of H5, gated by `risk.enforce.actionBinding`.
- Tests: `tests/unit/guardrails.test.mjs` (node:test, CI) and
  `tests/guardrails_test.py` (mirrors the patterns, runnable now).
