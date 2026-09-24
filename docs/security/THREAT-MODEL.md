# the control plane — threat model & security findings

Red-team of the initial build (v0.1). Status keys: **FIXED** (addressed in the
hardening pass), **OPEN** (documented, not yet fixed), **ACCEPTED** (residual
risk we knowingly carry at this stage).

> **Status lives in [`security/findings.yaml`](../../security/findings.yaml),
> not in this prose.** Every `closed` and `partial` finding there names a
> COMMAND that proves it, and `tests/findings_assert_test.py` runs them in CI —
> so "fixed" is falsifiable rather than asserted. This document keeps the
> reasoning, which is the part a ledger cannot hold.
>
> That split exists because seven status claims here drifted in one week,
> including two documents recording the same finding differently. A second gate,
> `tests/findings_crosscheck_test.py`, fails when this prose and the plan
> disagree.
>
> **What neither gate can do:** tell you an assertion is a GOOD one. A finding
> whose proof passes vacuously is still wrong, just wrong with a receipt.

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

### C1 — Pool-mode scope isolation is advisory, not structural  · CLOSED 2026-07-31
Every runner bind-mounts the **entire** `./workspace` (all scopes). The router
only sets the agent's `cwd`; it does not limit what the agent can read. Because
runners run full Claude Code with `--dangerously-skip-permissions`, a `Bash`
step (`cat /workspace/scopes/**/tenant-b/**`) reads any sibling scope. `cwd` is a
context/UX convenience, **not a boundary**.
- **Exploit:** an agent in a low-trust scope reads a confidential tenant's files.
- **Impact:** the marquee guarantee is false for pool mode; ethical walls between
  competing tenants are unenforced unless dedicated silo runners exist (they
  don't — see L3).
- **CLOSED 2026-07-31 by cutover.** One container per isolation root, each
  mounting *only* its own subtree. Proven on the running stack, not in config: a
  file written into `delivery` is **not readable** from the pooled runner — the
  path does not exist there, which is the difference between refused and absent.
- **The commons runner failed this on first deploy**, and that is the finding
  worth keeping. It mounted a business subtree WHOLESALE, swallowing a nested
  `pii:block` root, so the pooled runner could read the isolated scope. The
  conformance check missed it because it was one-directional — it asserted a
  mount was not INSIDE a foreign root and never that a mount did not CONTAIN
  one. Both directions are now asserted, and the containment check was verified
  to fail on the broken topology before the generator was fixed.
- **Both vendors as of 2026-08-01.** The claude side closed 2026-07-31; the
  gemini side followed. `gen_compose.py` emits `gemini-runner-commons` with the
  same mounts, `SCOPE_ROOT` and `PII_POLICY` as the claude commons runner, and
  the pool-mode runners that still mount the whole workspace now sit behind a
  compose profile so scoped mode never starts them. Leaving those running would
  have made the whole claim cosmetic: an idle container with total visibility on
  `agentnet`, one HTTP call away for anything holding `RUNNER_TOKEN`.
- **gemini is pool-only, and that is derived rather than chosen.** `silo` and
  `pii:block` create an isolation root AND put the data dimension at 3, which is
  where `blockCrossVendor` fires — so a root can never legitimately reach a
  second vendor, and a per-root gemini container would be idle by construction.
  Such a request escalates to the cheapest permitted claude tier.

### C2 — The router is not the single entry point  · CLOSED 2026-07-28
~~`fanout`, `incident-responder`, and `dependency-bumps` call the runner's
`/fanout` and `/run` **directly**~~, bypassing scope validation, ethical-wall
checks, the PII prompt guard, and tier clamping.

**Both halves of the stated fix landed 2026-07-28**, which is why this is closed
rather than mitigated:
- *Enforcement moved INTO the runner:* `SCOPE_ROOT` confines every path to its
  scope root, and `PII_POLICY` is enforced from the environment (never the
  payload, since a direct caller is exactly who that guards against).
- *All traffic forced through the router:* the router gained `/fanout` with the
  same gate order as `/route`, and every workflow was repointed.
- *Proven by:* zero direct-to-runner HTTP nodes in `n8n-workflows/` (asserted by
  `scripts/workflow_lint.py`'s caller checks and re-counted 2026-07-28), plus
  `tests/unit/runner.test.mjs` for the in-runner boundary.

**Residual closed 2026-08-01.** `gemini-runner` no longer carries scoped
traffic: routing goes to `gemini-runner-commons`, which has the pooled subtree,
a `SCOPE_ROOT` and a `PII_POLICY`. Verified by stopping that container and
watching t1 routing fail with `ENOTFOUND gemini-runner-commons` — the path is
the named scoped runner, not an inference from a successful response.

### C3 — n8n is the crown jewels and the most exposed surface  · FIXED (partial)
n8n was published on `0.0.0.0:5678` (LAN-reachable), default-no-auth, with env
access enabled in Code nodes.
- **Fixed:** bound to `127.0.0.1:5678`; README/compose now require completing
  n8n owner-account setup before use.
- **Still your job (OPEN):** enable n8n user management / SSO (or put IAP in
  front, per GSUITE-GCP.md) — a localhost bind stops the LAN, not a local user.
  `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` remains (the notify seam needs `$env`).
- **Correction 2026-09-17: the auth tokens ARE in n8n's env.** This entry said
  they lived in workflow node params, so the env exposure was only notify-webhook
  URLs. That stopped being true as the lanes grew. `LANES_TOKEN`,
  `ROUTER_TOKEN` and `RUNNER_TOKEN` are all set on the n8n service, and eight
  workflows read them through `$env`. With env access on, any expression or Code
  node can read them.
  - **`RUNNER_TOKEN` is the one that matters.** It is the runners' inbound
    credential, and n8n holds it only to verify the ask-human webhook
    (`ask-webhook`, `seam-heartbeat`). A workflow authored in the n8n UI can
    therefore call `/run` or `/fanout` on a runner directly and skip the router:
    no scope, ledger, kill switch or binding. `boundary_check.py` catches that
    shape in the repo's workflow files, but not in a workflow created in the UI.
    **Until fixed, n8n editor access is runner access.**
  - ✅ **Fixed in code 2026-09-17.** The ask-human door has its own secret.
    The runner's ask-human MCP presents `ASK_HUMAN_TOKEN` (`x-ask-human-token`),
    and `ask-webhook` and `seam-heartbeat` verify and probe with it. The n8n
    service no longer carries `RUNNER_TOKEN`, which the runners still use for
    router calls and their own loopback `/ask/status`. Every claude runner,
    hand-written and generated, carries the new token as required (`:?`).
    Two guards keep it from returning: `tests/security_test.py` fails if the n8n
    service's env names `RUNNER_TOKEN`, and `boundary_check.py` gained a third
    rule, **HOLDING**, that fails any n8n workflow file referencing it in any
    position. HOLDING also closed a loophole in the old CREDENTIAL rule:
    `seam-heartbeat` *sends* the runner token, but it passed because any file
    mentioning `$env.RUNNER_TOKEN` counted as verifying.
  - **Residual:** HOLDING covers workflow files in the repo, not a workflow
    typed into the n8n UI. After this change, though, there is nothing in n8n's
    env for such a workflow to spend. **Deployed 2026-09-18, and `RUNNER_TOKEN`
    rotated the same day.** Verified live: n8n's env has no `RUNNER_TOKEN`, and a
    runner answers 401 to the old value.
  - **The door's refusals are visible too (2026-09-18).** `ask-webhook` answered
    every POST with 200 "Workflow was started" (`responseMode: onReceived`)
    before its auth node ran. A wrong token, the runners' token and no token all
    got 200, and only n8n's log recorded the rejection. So the seam heartbeat
    could not tell a locked door from an open one: its 401 "BLIND" branch could
    never fire. The auth node now returns a verdict, and the webhook answers from
    it (`responseNode`): 401 bad or missing token, 503 n8n has no token, 400
    invalid request, 200 healthcheck, 202 question accepted. A refusal still
    throws after it has been answered, so the error workflow's alert on an
    unknown caller is kept. The heartbeat now requires `ok: true` in the body,
    so a regression to answer-on-receipt alarms instead of passing.
    `tests/unit/ask-door.test.mjs` runs the workflow's own Code-node JavaScript
    against each kind of caller; all 15 cases failed on the old door.
  - *Found by* reading the n8n container's env while investigating an unrelated
    boot warning, which printed the token into a session transcript. Rotate
    `RUNNER_TOKEN` when that happens; the value is shared by every runner and n8n.

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
  privilege denies tools a scope doesn't need (see GUARDRAILS.md). *Correction
  2026-09-17:* until that date the list went to `--allowedTools` and denied
  nothing under skip-permissions. This mitigation was cited here and did not
  exist (H5). Only finding
  types reach the audit ledger.
- **Still the real fix:** C1 — an agent that cannot read tenant-B cannot leak it.
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

### H5 — The risk model trusts a caller-declared verb, and the tool allowlist restricted nothing  · PARTIAL (binding ON 2026-09-18; gemini internal calls still unrestricted)
Two declarations of what an agent can DO, and neither was enforced.

**The declared verb.** The `access` dimension, which sets blast radius, comes
straight from the request: `R.access[input.action] ?? R.access.default`
(`router/server.js`, `classifyRisk`). An omitted `action` is scored, and recorded
in the ledger, as `advise`, the least risky verb. The operating-mode cap reads the
same field (`ACTION_RANK[input.action] ?? 1`). `requireVerify` fires at access ≥ 2
and `requireApproval` at access ≥ 3, so a caller that declares `advise`, or omits
the verb, skips both gates whatever the agent is about to do. An unknown verb
(`"deploy"`) falls to the same default. Counted 2026-09-17 across the
`/route` call sites in `n8n-workflows/`: both `incident-responder` calls and
`router-dispatch.example` omit `action`; `dispatch` forwards whatever its caller
sent; only `research-watch` declares one. `incident-responder` tells the agent to
write to `deliverables/` while declaring nothing.

**This is an unrecorded instance of a rule the plan already states.**
`ISOLATION-FIX-PLAN.md` (step 2) has the runners read `PII_POLICY` "from the
ENVIRONMENT, never the payload — a direct caller is exactly who this guards
against, so a caller-declared policy would hand the bypass back." The same
reasoning applies to `action`.

**The capability declaration was not a control either.** Actual capability is a
separate declaration: per-scope `allowedTools`, passed to `claude-runner`, which
appended `--allowedTools`. That flag adds permission ALLOW rules, and the runners
run with `--dangerously-skip-permissions`, which bypasses every permission check.
GUARDRAILS.md said "unlisted tools are effectively denied" under a verify-on-live
note that nobody had run.
- **Probed live 2026-09-17** (claude 2.1.220, `claude-runner-commons`):
  `--allowedTools Read` still ran Bash. `--tools Read` left only Read, but
  `--tools` covers only the built-in set: the same session still exposed
  `mcp__ask-human__ask_human` and seven `mcp__n8n__*` tools.
  `--disallowedTools mcp__ask-human` removed that server. The compliance map had
  rated tool allowlisting **full** coverage for OWASP-agentic T2.
- **`gemini-runner` never read the list at all.** It runs `--yolo`.
- **An empty list meant everything.** `sanitizeAllowedTools([])` returned
  `null`, meaning "pass no flag", so the most restrictive list anyone could write
  granted the full toolset.

**Why it is High and not Critical.** Today's callers are internal n8n lanes, so
this is defense-in-depth: a careless lane under-declares and skips approval.
There is no open door yet. [ADR 0018](../decisions/0018-intra-session-model-routing.md) plans to let
**runners** originate `/route` calls, and a verb self-declared by a less-trusted
caller becomes a real bypass at that point. ADR 0018 build step 3
(runner-originated requester identity) depends on this.

**Fix: bind the verb, don't just score it.** Deriving a risk floor from
`allowedTools` was the first design and was dropped: the list restricted nothing,
and no live scope declares one. With no list, the true floor is `execute` on
every request, so the floor would have put every lane behind approval. The fix
narrows each request's tools to what its effective action permits. A request
declared as `advise` then *cannot* write, and scoring on the verb becomes true.
- ✅ **Runner half, CLOSED 2026-09-17.** `claude-runner` enforces the list
  with `--tools` for built-ins and `--disallowedTools mcp__<server>` for every
  MCP server the list does not name. It refuses (422) what it cannot enforce:
  permission specifiers, tool-level MCP names and unknown servers. `[]` now
  means no tools. `gemini-runner` refuses any list with a non-read tool, and
  refuses a read-only list unless `READONLY_ARGS` is configured, because plan
  mode has not been probed headless. `policy.json → risk.toolAccess` classifies
  every tool as a verb, and unlisted tools fail closed to `execute`. *Proven
  by:* `tests/unit/runner.test.mjs` (14 tests, all failing against the
  previous runner) and `tests/security_test.py`.
- ✅ **Router half, BUILT 2026-09-17, gated off.** `router/actions.js`
  `bindAction()` yields one governing verb, and both `classifyRisk` and the mode cap read
  it. Named operations per scope (ADR 0019, `OPERATIONS.md`): an undeclared
  one is refused, a declared verb may raise access but never lower it, and
  `irreversible` forces approval. Under `block` an omitted or unknown `action`
  is a 400. Dispatch tools are narrowed to the governing verb: a bound
  `advise` against a `Read/Edit/Bash` scope dispatches `["Read"]`. `/fanout`
  now runs the risk and mode gates it never ran, and intersects each task's
  list with the decided one. The ledger records `action`, `declaredAction`
  (null when omitted), `operation`, `actionBound`, `actionUnderDeclared` and
  `toolsBound`. *Proven by:* `tests/unit/actions.test.mjs` and
  `tests/unit/router.test.mjs` (CI), plus `scripts/conformance_test.py` for
  operation declarations. Spot-checked independently against the combined
  tree.
- ✅ **Binding ON, 2026-09-18.** `risk.enforce.actionBinding` is `block` in
  the live policy. The ledger could not supply the evidence alone: 3 decisions
  in the day after binding went live, all smoke tests, because the live lanes
  barely route. The evidence was a static audit of every `/route` and `/fanout`
  caller, which `workflow_lint.py` now enforces for every repo workflow.
  Verified on the live router:
  - an omitted `action` → 400;
  - `trigger: "schedule"` and `"dispatch"` → 400;
  - `research-watch`'s request shape passes, bound to read-class tools;
  - a bound `advise` dispatched to claude with only `Read, Grep, Glob, LS,
    NotebookRead, TodoWrite, mcp__ask-human`, and the runner accepted it;
  - `execute` in a human-led scope → approval required.
- ✅ **Same bug class, other field — closed.** `trigger` is caller-declared
  too, and an undefined value fell to autonomy 1, a human turn.
  `research-watch` sent `"schedule"`, and `dispatch.py` sends `"dispatch"`.
  Under `block`, `bindAction` now refuses a trigger the policy does not define.
  A schedule- or webhook-started lane that omits `trigger` fails the lint. A new
  value, `event` (2), covers a run started by an external system: unattended,
  but not self-initiated. `incident-responder` declares it.
- **Residual (why H5 stays PARTIAL):**
  - **Gemini still runs `--yolo`.** `risk.enforce.secondVendorReadOnly` is
    `false`, so all bound work stays on claude. But triage and the verifier
    send gemini no tool list, so those internal calls still run with every tool
    there. That path closes only when `READONLY_ARGS` is probed against an
    authenticated gemini-runner and the key is `true`.
  - ✅ **`dispatch` fixed 2026-09-18.** It had never routed a ticket: it sent
    no `prompt`, which the router refused with 400 even under `warn`. It also
    sent `action:"advise"` for implementation work and `trigger:"dispatch"`.
    It now builds the prompt from the handoff (objective, acceptance, context)
    and passes acceptance to the verifier as `goal`. Verbs and task types come
    from the router's own policy, mounted read-only into `lanes`: `write` for
    file-changing task types, `advise` otherwise, and the handoff may state its
    own. The trigger is `scheduled`. An unknown verb or task type blocks the
    ticket BEFORE it is claimed, instead of being guessed. The workflow now reads
    the router's answer, so a refusal or a control stop on a claimed ticket
    raises a notice instead of ending silently.

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
- **M3 — HALF FIXED 2026-07-28. Read the remaining half; it is the more
  interesting one.**
  - ✅ **`fanout` `repo` escape — CLOSED.** It used `path.join(WORKSPACE, repo)`,
    which resolves `repo:"../.."` outside the workspace and returns it. Now
    routed through `safeCwd`, the same helper `/run` had always used in the same
    file. Regression test asserts the *thrown message*, because the vulnerable
    version also threw — later, from git, after touching a path outside the
    workspace — so "it rejects" would have passed on the bug.
  - ✅ **CLOSED 2026-07-28 (later the same day): `/run` no longer trusts the
    caller's `cwd` beyond its own root.** Both runners now read `SCOPE_ROOT` and
    refuse any path outside it — including a *valid, existing* path belonging to
    another tenant, which is the case that was accepted before. `gen_compose.py`
    had been emitting `SCOPE_ROOT` per generated runner since the isolation work
    began and **nothing read it**: the compose file advertised per-tenant
    isolation that no code enforced. An env var nobody reads is a comment.
    Verified by disabling the guard in a scratch copy — exactly the two
    cross-tenant tests fail, 19 others still pass. Unset `SCOPE_ROOT` keeps
    whole-workspace access for the commons runner and now says so at boot,
    because "isolated" and "not isolated" must not look alike in a log.
  - ~~STILL OPEN: `/run` trusts the caller's `cwd` within the workspace.~~
    `safeCwd` enforces a *workspace* boundary, not a *scope* boundary, so a
    token-holder can name any scope directory that exists and the runner will
    work in it. **Confining to the workspace is not the same as confining to
    your scope**, and the fix above does nothing for this.
    *Why it is not a one-liner:* the runner has no idea which scope the caller
    was granted — that lives in the router. Closing it means the runner stops
    accepting a caller-supplied path and instead derives it from something the
    router signs, which is the same "move enforcement into the runner" change as
    **C2**. Correctly folded into Phase 1; the escape above was not.
- **M4 — Cross-provider vendor exposure. ⚠ THE PRIOR "MITIGATED" WAS TOO BROAD
  (corrected 2026-07-28).** `blockCrossVendor` (data ≥ 3) keeps the **verifier**
  on the same vendor — that part is true and works. But it was read ONLY in the
  verify branch, so it never governed the **primary** dispatch.
  - **Verified against the running router:** scope `engineering` (`pii:block`,
    data = 3, risk = high) computes `blockCrossVendor: true` and was still
    dispatched to **`t1` / `gemini-2.5-flash-lite`** as its primary runner. The
    flag fired and was ignored. The full work went to vendor two — a larger
    exposure than the review prompt the control was written to stop, and the
    policy comment says "don't send confidential work to a 2nd vendor".
  - **Now enforced on every dispatch path** via `targetFor`, which refuses a
    non-claude provider when the flag is set. **Gated behind `RUNNER_MAP=on`**, so
    it lands with cutover as one decision rather than silently changing routing
    today. Consequence: a confidential scope loses the cheap `t1` tier and moves to
    a claude tier, which costs more. **It was refused outright rather than moved**
    until 2026-07-31 — routing now escalates to the cheapest PERMITTED tier and
    records `vendorEscalated` on the decision and in the ledger, with the 403 kept
    only as a backstop. A refusal issued BY a security control is
    indistinguishable from that control working, which is how it survived cutover.
  - **Residual CLOSED 2026-08-01.** The shared `gemini-runner` mounted the WHOLE
    workspace with `--yolo` and no `SCOPE_ROOT`. Second-vendor traffic now goes to
    `gemini-runner-commons` — pooled subtree, `SCOPE_ROOT`, `PII_POLICY` — and the
    whole-workspace pool runners sit behind a compose profile so scoped mode never
    starts them. `--yolo` remains, now bounded by the mount rather than by trust.
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
- **M7 — No CPU/mem/pids limits.**  · **CLOSED 2026-07-28.** All 8 base
  services and all 5 generated silo runners declare `mem_limit`,
  `memswap_limit`, `cpus` and `pids_limit`. `pids_limit` is the control that
  actually stops the fork bomb; `memswap_limit == mem_limit` prevents escape
  into host swap. *Proven by:* `tests/security_test.py` per service across both
  compose files, and `docker inspect` on the running containers — declared and
  enforced are different claims.
- **M8 — Secrets in env** (visible to `docker inspect`, `/proc`, n8n Code nodes).
  *Fix:* docker secrets / mounted files.

## Low  · OPEN

- **L1 — No durable audit log in the router.**  · **CLOSED.** The `decision`
  block is appended to `/audit/decisions.jsonl` as a hash-chained record
  (`router/server.js`). *Proven by:* `scripts/verify_audit.py` — 22 records,
  chain intact, re-run 2026-07-28. This row said OPEN for weeks after the plan
  recorded it fixed; the cross-check gate is what surfaced the disagreement.
- **L2 — 500 responses echo internal paths** (info disclosure).
- **L3 — silo `runnerHost`s in `scopes.json` don't exist in compose** — silo
  scopes currently fail (but fail *closed*).

---

## Diagnosability

### D1 — A runner error was reported as a successful, empty decision  · CLOSED 2026-08-01
`callRunner` resolved on **any** HTTP status, so a runner 500 became a `200`
whose result was an empty string — and the audit ledger recorded it as a
successful decision. Not a confidentiality or integrity failure, which is why it
sat below the other findings; it is an *observability* one, and it hid a total
outage.

- **How it surfaced.** The first live run of a curated eval set. Both vendor CLIs
  were failing (neither runner is logged in), every `/route` returned `200`, and
  the only visible symptom was empty output. Earlier in the same session, `200`
  responses from `/route` had been read as evidence that work executed — they
  were evidence that *routing* worked, and nothing more.
- **Why it stayed hidden.** Two independent vacuities lined up. The router turned
  a failure into an empty success, and the eval's negative graders
  (`not_regex`/`not_contains`) are satisfied by an empty string, so the case that
  should have caught it reported PASS. A rule of the form "must not contain X" is
  trivially true of nothing.
- **Fixed on both halves.** The router rejects on status ≥ 400, preserving a 4xx
  policy refusal (the runner's own scope/PII guards) and mapping 5xx to `502`, so
  "the runner is broken" is distinguishable from "the model had nothing to say".
  Negative graders now FAIL on empty output. Proven before and after on the
  running stack: `200`-with-empty-result became `502` naming the host and the
  underlying CLI error.
- **Cause found, and it was not a missing login.** `CLAUDE_CODE_OAUTH_TOKEN`
  was already set in `.env`; `gen_compose.py` never passed it to the generated
  runners. Cutting over from the hand-written `claude-runner` to the scoped ones
  therefore unauthenticated every claude tier, and D1 hid the symptom. **A
  generated service must reach env parity with the one it replaces** — omitting a
  variable is not a smaller config, it is a different one. Fixed and verified end
  to end: `loggedIn: true`, a real API call, real token usage, `is_error: false`.
- **Also fixed:** the auth volume covered `/home/node/.claude` while the CLI
  writes `.claude.json` *beside* it, so config and session state were lost on
  every recreate. Proven by planting a marker in each location and recreating —
  the one inside the volume survived, the one beside it did not. A home-level
  volume now covers both. The first attempt patched only `docker-compose.yml`
  and the generated runners kept the old shape, which is the same split that lost
  the token.
- **Residual (OPEN):** `gemini-runner` has no auth method — it needs
  `GEMINI_API_KEY` (or Vertex/GCA), which is absent from `.env` and is the
  operator's to supply.

### D3 — A prompt reached the append-only audit ledger  · CLOSED 2026-08-06
The ledger's first line says it: *"Metadata ONLY — never the prompt (a SHA-256 +
length instead)."* That is what makes it safe to keep indefinitely, and it is
what the no-external-processor position rests on.

**Fixing D1 broke it.** Propagating runner errors — so a failing runner stops
reporting as an empty success — carried the runner's stderr into the `Error`
message. A failing CLI reports `Command failed: claude -p <the entire prompt>
--flags`, and `route()`'s handler writes that message into `blocked`. A fix for
one diagnosability bug created a confidentiality one, which is the argument for
checking what a change *writes* and not only what it reports.

- **Found by building a status page**, not by review: dumping `blockedReasons`
  from the ledger showed a prompt sitting in a category key.
- **Fixed by `runnerErrorSummary`** — first line only (stderr tails are where
  prompts hide), everything from the `-p` flag replaced with `<prompt omitted>`,
  hard cap at 200. Verified against the exact leaked string, the gemini form, a
  multiline stderr, and a non-prompt error that must still diagnose (over-
  scrubbing would hide the `GEMINI_API_KEY` message an operator needs).
- **Residual, accepted by the owner 2026-08-06:** two records already contain a
  prompt and **cannot be removed** — append-only and hash-chained is the point.
  Both were test prompts from this session, no client content. Leaving them and
  recording the incident was chosen over breaking the chain. Rotating the ledger
  with a documented break is the remedy if a real prompt ever lands this way.

### D2 — A dead verifier destroyed completed work  · CLOSED 2026-08-01
`verifyOnTiers: ["t3"]` sends every frontier decision to a **cross-vendor**
reviewer. When that vendor was unreachable the exception propagated out of
`route()`, so the primary work — already executed, already paid for — was
discarded and the caller got a 502. One unauthenticated second vendor took down
every `t3` request in every non-confidential scope.

- **Found by testing, not by review.** A realistic `plan` task in `internal`
  returned 502 naming `gemini-runner-commons`; a `summarize` in the same scope
  succeeded, which isolated the fault to the verify branch rather than dispatch.
- **The fix is a distinction the code did not previously draw.** A verify
  demanded by the risk model (`requireVerify`) or asked for explicitly is a
  **control** — if it cannot run, blocking is correct. A verify triggered by the
  tier or task-type table is a **quality heuristic** — if it cannot run, the work
  should be returned with the verdict marked missing. Both used to fail
  identically, and identically badly.
- **Same-vendor fallback first.** Most of a fresh-context reviewer's value is the
  fresh context, not the second brand, so an unreachable cross-vendor reviewer
  now retries on the primary vendor and records `degraded-same-vendor` — weaker
  than the independent check and labelled as such.
- **`verifyStatus` reaches the ledger**, because `verdict: null` conflated "no
  verify required" with "the reviewer was unreachable". Without it
  `eval_metrics`' verify-pass rate would quietly *improve* during an outage.
- **Proven both ways.** Live: the degraded path, against the real gemini outage,
  with the ledger recording it and the hash chain intact. By test: the
  both-vendors-down branches, which are near-unreachable live because the
  fallback targets the same runner that just succeeded — `verifyOutcome` is
  extracted and unit-tested, and both tests were confirmed to fail against the
  two obvious regressions (always-degrade, and always-block).

---

## What's actually solid
The vault-not-in-runners split (H2 notwithstanding) and the human gates on
irreversible acts (ship, promote, apply-fix) are the right shapes. The isolation
model is the weak leg — and it's the product — so **C1 → C2 are the deep fixes
to schedule before real tenant data touches this.**

## Remediation order
1. **C3** — lock n8n (done: localhost bind; you: enable owner auth). ✅ pass 1
2. **H1** — per-service tokens, fail-closed, constant-time. ✅ pass 1
3. **H4** — fail-closed firewall + DNS restriction. ✅ pass 1
4. **C1** — real per-scope mounts (silo runners). ⟵ next deep change
5. **C2** — enforcement inside the runner.
6. **H2 / M1** — rehydrate token + vault encryption.
