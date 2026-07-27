# Market analysis — where Brightworks fits

Research of the adjacent 2026 markets, and an honest read of where Brightworks is
differentiated vs. where the market is ahead. Sources are external write-ups
(treat vendor/analyst comparisons directionally).

## The market splits into five categories — Brightworks spans four

| Category | Leaders (2026) | What they do |
|---|---|---|
| **Governance platforms** | Arthur AI, Credo AI, Fiddler, Microsoft Agent Governance Toolkit | Discovery, runtime guardrails, continuous eval, compliance evidence (~$3.4B market, BFSI-led) |
| **AI gateways** | LiteLLM, Portkey, Kong AI Gateway | Routing, failover, caching, budgets, per-team cost, PII redaction |
| **Orchestration** | LangGraph, CrewAI, AutoGen, Google ADK, OpenAI Agents SDK | State graphs, checkpointing, human-in-the-loop primitives |
| **Agent security** | Lakera, Nightfall, AWS Bedrock / Azure Guardrails | Prompt-injection detection, PII entities, jailbreak shields |
| **Agent work management** ⟵ *we do not play here* | **Paperclip**, Devin/Cognition's task surface, ticket-system bolt-ons | The unit of work: tasks with identity and assignee, org charts, goal ancestry, execution leases, per-agent budgets, resumable sessions, human review UI |

Almost every product is *one* of these. Brightworks is simultaneously a gateway
(tiering router), orchestrator (n8n lanes), governance platform
(audit/risk/mode/RBAC/conformance), and security layer (isolation/guardrails/PII)
— self-hosted, in one stack. That breadth is the differentiator.

**The fifth category was a blind spot in this analysis until 2026-07-25.** It is
not the same thing as orchestration: orchestration is *how a run executes*, work
management is *what the run is for, who owns it, and how a human sees it*.
Brightworks has lanes but no unit of work — no task with identity, no assignee,
no execution lease, no resumable session, no human review surface beyond n8n's
execution list. That is now tracked as E4–E6 below and planned in
[GAP-CLOSURE-PLAN.md](GAP-CLOSURE-PLAN.md).

### Paperclip — the category leader, assessed

[paperclipai/paperclip](https://github.com/paperclipai/paperclip) — MIT, **74.7k
★**, 13.9k forks, created **2026-03-02**, 3,264 commits (verified via `gh api`,
2026-07-25). Node + React + Postgres. Framing: *"If OpenClaw is an employee,
Paperclip is the company."* Brings its own control plane — identity, org chart,
work/tasks, heartbeat execution, governance/approvals, budgets, routines,
plugins, secrets, activity, company portability — over BYO agents (Claude Code,
Codex, Cursor, bash, HTTP bots).

**What it does that we cannot:** atomic task checkout with execution locks (no
double-work, no double-spend, in one transaction); heartbeats that resume the
same task context across wakeups; cost attributed by company/agent/project/goal/
issue/provider/model with auto-pause *and queue cancellation* on overspend;
approvals bound to authenticated users via run JWTs; orphaned-run recovery;
shipped sandbox adapters for e2b / Cloudflare / Daytona / Modal / Novita /
self-hosted k8s; a governed MCP tool gateway; org export/import with secret
scrubbing.

**Where we stay ahead:** it has no threat model, no tamper-evident ledger, no
risk model, no regulatory mapping, no PII tokenization pipeline, and no
structural-isolation story. Its governance is application logic over Postgres —
real, but not provable. Ours fails CI when an invariant breaks.

**Why we are not adopting it as the control plane** (`build-vs-adopt`,
`dependency-security`):

1. **Telemetry is enabled by default** (`PAPERCLIP_TELEMETRY_DISABLED=1` /
   `DO_NOT_TRACK=1` to opt out). Disableable, so a configuration gate rather than
   a veto — but default-on egress to a vendor contradicts row 5 below, which is
   the whole positioning.
2. **Maintenance surface:** 4,945 open issues and **2,919 open PRs** on a
   five-month-old repo. The `dependency-security` privileged-path principle says
   the bar scales with position, not popularity — and a control plane is the most
   privileged position there is. 74.7k ★ is explicitly not the metric that clears
   it.

**Verdict: adopt the ideas, not the dependency.** Read its sandbox adapters
before writing the C1 silo runners; take its work model; leave its framing.

> **Framing caveat.** Paperclip's pitch is autonomous AI companies and org charts
> — *"if you have twenty [agents], you definitely do [need this]."*
> [LADDER.md](../design/LADDER.md) says the opposite: agent count describes shape,
> not success; never gate on raw agent count. Take the work model, not the
> scoreboard.

## Where Brightworks is genuinely unique or ahead

1. **Subscription/free-tier billing via CLI-wrapping.** Every gateway assumes
   metered API keys and sells savings on that spend. Brightworks avoids API spend
   entirely by riding the Claude subscription + Gemini free tier through the CLIs.
   Orthogonal to the whole gateway value prop — nobody there does this.

   > ⚠️ **This row is contradicted by our own verified register and needs an
   > owner's call.** `idea-dossier/00-index.md` records, marked **Verified**
   > (2026-07): *"Claude automation on subscription auth moved to metered credit
   > pools (Jun 15 2026) — flat-subscription no longer covers headless/agent
   > volume,"* and `idea-dossier/06-backlog.md` reconciles the differentiating
   > claim to **dead** on exactly this clause. The README's opening still leads
   > with flat-subscription billing too. Either the dossier's finding is wrong, or
   > this is no longer differentiator #1 and both documents overstate the position.
   > **Left in place rather than silently rewritten** — re-verify against current
   > Anthropic terms and then correct in one pass. Tracked as a Phase 00 item in
   > [GAP-CLOSURE-PLAN.md](GAP-CLOSURE-PLAN.md).
2. **Governance-as-code with a conformance test suite.** The market ships
   governance as dashboards + policy docs + continuous eval. Brightworks
   *unit-tests its invariants* — the structural isolation proof, ethical walls,
   mode monotonicity fail CI if violated. Rare-to-absent commercially.
3. **Structural isolation (OS mounts) over filtering.** The market's own
   consensus is that injection is unsolved at the model layer, so the strategy is
   *containment*. That's the C1 thesis — but Brightworks goes past prompt/response
   filters to filesystem mounts that survive `--dangerously-skip-permissions`.
4. **Unified risk × mode at one chokepoint.** Fuses NIST-style risk tiering
   (data×access×autonomy) with the operating-mode axis (who leads/validates),
   derives the human gate from the score, and records it in a tamper-evident,
   PII-free hash-chained ledger. The market has these as *separate* products.
5. **Local-first, no SaaS, no per-seat, no external data processor.** Governance
   platforms are SaaS (Credo/Arthur/Fiddler); gateways run $10K–200K/mo. For a
   consultancy under client NDAs, "no third party ever sees the data" is a real
   edge, not a footnote.

## Where the market is ahead — prioritized enhancements

| # | Gap vs market | Who does it | Fit | Status |
|---|---|---|---|---|
| E1 | **Framework-coverage matrix** (OWASP Agentic Top 10 / NIST AI RMF / EU AI Act mapping) | Microsoft toolkit | High, cheap | ✅ done — `compliance/framework-map.yaml` + `scripts/gen_compliance_map.py` → `docs/compliance-map.md`, regression-tested by `tests/compliance_test.py` (status corrected 2026-07-25; the row said "queued" after the work had shipped) |
| E2 | **Continuous eval + drift** (quality/task-adherence over time) | Arthur, Fiddler, Galileo | High | ✅ done — eval/drift lane derives agentic-leverage + verify-pass/change-failure/block/mix metrics from the audit ledger, trended with drift WARNs (`scripts/eval_metrics.py`, weekly n8n lane; see EVAL.md). The semantic/ground-truth complement is now built too — the **agent improvement loop** (offline quality regression: prod failure → tokenized capture → offline eval against per-scope datasets → fix; `scripts/eval_run.py`, `scripts/eval_capture.py`, see IMPROVEMENT-LOOP.md), which closes the offline-quality-regression backlog. Both are **local + governed** (per-scope, PII-tokenized, retention-bound, no external data processor) vs the SaaS eval platforms — the same edge as row 5 below. |
| E3 | **ML-based injection/PII detection** (trained classifiers, 50+ PII entities) | Lakera, Bedrock, Presidio/DLP | High | ✅ done — pluggable detector seam (`SCRUB_BACKEND` presidio/dlp; `guardrails.backend` lakera/bedrock) behind the heuristic default + fail-safe fallback, no new deps; closes M2/H3 when enabled (see DETECTION-BACKENDS.md). |
| **E4** | **A unit of work** — task with identity, assignee, parent/goal ancestry, thread, and an **atomic execution lease** so two lanes cannot claim the same target | Paperclip | High | **decided — [ADR 0010](../decisions/0010-agent-work-management.md)**: build the task record + lease at the router chokepoint; do **not** adopt Paperclip (a peer control plane makes C2 permanent) or a durable-execution engine yet. Resumable execution deferred to DBOS with a trigger; the human work-surface stays as-is until a second operator. Build in [GAP-CLOSURE-PLAN.md](GAP-CLOSURE-PLAN.md) phase 2 |
| **E5** | **Authenticated approval ingress + principal identity** — approvals bound to a verified human, not a bearer URL; user identity carried into tool calls | Paperclip (run JWTs); every governance platform | High | planned — phase 3. This is threat-model **M5**'s remaining half |
| **E6** | **Cost attribution below scope** — spend by project/goal/task/model, warning thresholds, and **cancelling queued work** on overspend (we only refuse *new* work) | Paperclip, Portkey, LiteLLM | Medium | planned — phase 4 |
| **E7** | **Span-level distributed tracing** (UI → gateway → agent → tools → model) | Phoenix/Arize, Langfuse, Paperclip (opt-in OTel) | Medium | planned — phase 4. `agentsview` + the ledger give per-decision records, not spans |
| — | Compliance-evidence exporter (GDPR/HIPAA/SR 11-7 packs) | Fiddler | Medium | backlog |
| — | Agent registry / shadow-AI discovery (runtime) | Credo AI | Medium | backlog (static register exists) |
| — | Semantic caching | Portkey | Low | needs embeddings — deliberately avoided |
| — | Broad provider support (100–250 models) | LiteLLM, Portkey | Low | by-design cost of the subscription model |

## Positioning

**The self-hosted, unit-tested governance-and-orchestration stack for the firm
that won't send client data to a third-party governance SaaS and won't pay
metered API rates.** The market bifurcates into expensive external-processor SaaS
and ungoverned gateways/frameworks; that niche — boutique consultancies, law,
healthcare, anyone under strict data-processor constraints — is real and
underserved.

The fifth category does not change that positioning — it changes the *shape of
the product*. A firm that will not send client data to a governance SaaS still
needs to know which task an agent is working, who approved it, and what it cost.
Today they read that off an n8n execution list. Closing E4–E6 is what turns "the
governed stack" into something an operator can actually run a week of work
through.

### The boundary — adopt components, decline peers

The rule that came out of assessing Paperclip, and the one that keeps
"integrate, don't rebuild" from collapsing into "adopt the biggest thing":

> **Adopt a *component*. Decline a *peer*.** A component owns one layer and
> exposes it (LiteLLM, Langfuse, Presidio, DBOS) — adopting one *subtracts*
> maintenance and leaves the enforcement point where it was. A peer owns identity,
> approvals, budgets and secrets end to end (Paperclip, Portkey, agent-fleet-o) —
> adopting one *adds* a second authority over the same actions, and "which system
> decided this was allowed?" stops having one answer.

The single enforcement point **is** the product: the tamper-evident ledger, risk ×
mode, ethical walls and the conformance suite all depend on there being exactly
one place a decision is made. A second control plane does not add a feature, it
removes the property. Stated at length in
[`idea-dossier/05-positioning.md`](../../idea-dossier/05-positioning.md).

The honest cost: holding that boundary means lagging the peers on **operator
experience** — work surface, review UI, mobile — for as long as it holds. Right
while the operator count is one; ADR 0010 names the trigger that re-opens it
(a *front-of-router* integration at operator two, never a replacement).

*Sources: 2026 comparison write-ups from arthur.ai, superblocks.com,
truefoundry.com, konghq.com, nightfall.ai, ecorpit.com, and the NIST AI RMF /
Databricks DASF / OWASP Agentic frameworks. Vendor/analyst material treated
directionally; date-check before quoting. Paperclip figures verified directly
against the GitHub API on 2026-07-25, not from a write-up. The E4–E7 rows also
draw on Micheal Lanham, "AI Agents in Action" 2e (Manning, 2026) §8.3–8.4 and
ch. 7; the full read is in
[EXTERNAL-ASSESSMENT-2026-07.md](EXTERNAL-ASSESSMENT-2026-07.md).*
