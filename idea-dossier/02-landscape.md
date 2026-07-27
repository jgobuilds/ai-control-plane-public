# 02 — Landscape

*Research snapshot July 2026. Public sources; verify before quoting.*

The wedge — "reliability / governance / cost-control layer for agentic systems" —
is **not white space.** It is a busy intersection of three mature-ish clusters,
plus a fast wave of open-source projects landing on Brightworks' exact
combination.

| Cluster | Representative tools | What they do | Compete / avoid / integrate |
|---|---|---|---|
| **AI gateways / routers** | LiteLLM (MIT), Portkey, Kong AI Gateway, Cloudflare AI Gateway, OpenRouter | Multi-provider routing, per-key **budgets**, rate limits, caching, PII redaction (Kong) | **Integrate** — this is Brightworks' router, already commoditized |
| **Agent observability / eval** | Langfuse (MIT open-core), LangSmith, Arize Phoenix, AgentOps, Helicone, Datadog | Tracing, evals, cost, session replay, audit-ish | **Integrate** — this is Brightworks' ledger/eval, already commoditized |
| **Agent governance / control plane** | Portkey, Databricks Unity AI Gateway, CHEQ, Gravitee, **Keel**, Microsoft agent-governance-toolkit | Policy gates, approval, budgets, immutable audit | **Compete (crowded)** — Brightworks' core, and occupied |
| **OSS governance projects** | agentward, Agentic Control Plane (ACP), agent-fleet-o, AgenticAudit, open-managed-agents | Self-hosted policy + audit + PII + HITL + budget ledger | **Direct overlap** |
| **Agent work management** *(added 2026-07-25)* | **Paperclip** (MIT, 74.7k ★), ticket-system bolt-ons | The unit of work: tasks with identity and goal ancestry, org charts, execution leases, per-agent budgets, resumable sessions, a human review UI | **Decline** — see below. Not a component we can integrate; a competing control plane |

## Closest analogs

**Keel** ([keel-ai](https://pypi.org/project/keel-ai/), reviewed 2026-07) — "agent-governance
compliance middleware… wraps agent tool calls so every action is evaluated
against policy before it runs." **This is Brightworks' one-line positioning,
already shipped and already named Keel.** Different mechanism (Python SDK wrapper
vs n8n/router), same job. *Disqualifies the name and crowds the position.*

**agent-fleet-o** ([GitHub](https://github.com/escapeboy/agent-fleet-o), 2026-07) — self-hosted
"mission control": visual DAG workflows (n8n-shaped), 450+ MCP tools, HITL
approvals, **risk policies, audit trail, budget controls with a real credit
ledger**, works with Claude/GPT/Gemini/Ollama/Codex. **Remarkably close** to the
whole Brightworks combination, open source.

**LiteLLM** ([pricing](https://www.truefoundry.com/blog/litellm-pricing-guide), 2026-07) — MIT.
The router + cheapest-tier + per-key/user **budget ceiling** + Docker, with 100+
providers and Langfuse/OTel hooks. **Commoditizes Brightworks' router.**

**Paperclip** ([paperclipai/paperclip](https://github.com/paperclipai/paperclip),
assessed 2026-07-25 — figures from the GitHub API, not a write-up) — MIT, **74.7k ★**,
13.9k forks, created **2026-03-02**, 3,264 commits, **2,919 open PRs**. Node + React
+ Postgres. *"If OpenClaw is an employee, Paperclip is the company."* Brings tasks
with goal ancestry, atomic checkout with execution locks, heartbeat-resumable
sessions, per-agent budgets with queue cancellation, run-JWT-bound approvals,
shipped sandbox adapters (e2b / Cloudflare / Daytona / Modal / Novita / k8s), a
governed MCP tool gateway, and org export/import.

**This is the one cluster where the "integrate, don't rebuild" instinct does not
apply**, and it is worth being precise about why — it sharpens the whole
positioning. Paperclip's own README says *"a full control plane, not a wrapper."*
LiteLLM and Langfuse are **components**: they do one layer and expose it. Paperclip
is a **peer** — it owns identity, approvals, budgets and secrets end to end.
Integrating a component subtracts maintenance; integrating a peer creates two
authorities over the same actions, which is precisely threat-model **C2** made
permanent. Decision recorded in
[ADR 0010](../docs/decisions/0010-agent-work-management.md).

### Convergence
Multiple independent teams reached Brightworks' exact principles — policy-gate,
budget-ceiling, immutable audit, HITL, PII redaction, self-host. That validates
the *thinking* and simultaneously proves it is **not novel**.

### Structural gaps we accept
- No dedicated tracing UI (Langfuse/LangSmith are years ahead).
- No 100+ provider matrix (LiteLLM's core competency).
- No multi-tenant hosted control plane (Portkey/Databricks).
- **No unit of work, no human work-surface, no durable/resumable execution**
  (Paperclip; Temporal/DBOS for the durable half). A minimal task record + lease
  is being built at the router chokepoint per ADR 0010; the review UI and durable
  replay are deferred with triggers, not claimed.

## Why now — and against us

The forcing function is real (EU AI Act high-risk obligations enforce
[Aug 2 2026](https://zylos.ai/research/2026-05-01-ai-agent-governance-compliance-2026/):
human oversight + 6-month log retention). But "why now" cuts **against** a new
entrant: the demand is obvious, so the space filled *fast* and is now contested by
funded vendors and a dozen OSS projects. Brightworks is arriving late to a party
it correctly predicted.

Sources: [Contabo gateways](https://contabo.com/blog/litellm-vs-ai-gateways/) ·
[Galileo observability](https://galileo.ai/blog/best-ai-agent-observability-platforms) ·
[awesome-ai-agent-governance](https://github.com/systempromptio/awesome-ai-agent-governance) ·
[Domo governance tools](https://www.domo.com/learn/article/ai-governance-tools)
