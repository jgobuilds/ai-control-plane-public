# Brightworks — idea dossier

**This folder is decision support, not product documentation.** It vets whether
Brightworks should become an open-core *product* and rebrand. It is built to
disconfirm; a validation-only pass would be worthless.

Research snapshot: **July 2026**. Pricing/features from public sources — verify
before quoting externally.

## Status

| File | State |
|---|---|
| `00-index.md` | Draft |
| `01-concept.md` | Draft |
| `02-landscape.md` | Researched — **updated 2026-07-25**, fifth cluster added |
| `03-viability.md` | Researched |
| `04-clearance.md` | Researched — **load-bearing finding** |
| `05-positioning.md` | Researched — naming space saturated |
| `06-backlog.md` | Draft |

## Headline

**The product/commercial ambition does not survive the vet.** The category is
crowded, Brightworks' differentiators are commoditized (LiteLLM, Langfuse) or
were closed by a vendor change (Anthropic metering, Jun 2026), the *exact*
positioning is already a named product (**Keel**), and the open-core commercial
path is **blocked by n8n's Sustainable Use License**. Brightworks remains
valuable as an *internal* harness and a plausible *source-available* release —
not as a product to sell. Detail below and in the owning files.

## Decisions log

- **2026-07-24** — Ran the vet. Verdict: **do not pursue the open-core commercial
  product as framed.** Rebrand is cheap/fine for an internal or OSS release, but
  the evocative-name space is saturated (every metaphor checked is taken, several
  in-category) — go coined, and clear fully. Reframe, if persisting, from
  "another agent-governance platform" to "opinionated *reference stack* that
  integrates the existing components + the engineering doctrine."
- **2026-07-25** — Assessed **Paperclip** (MIT, 74.7k ★) and Lanham's *AI Agents
  in Action* 2e against the stack. Added a **fifth cluster to the landscape —
  agent work management** — which the vet had missed entirely and in which we do
  not play. Declined to adopt Paperclip ([ADR
  0010](../docs/decisions/0010-agent-work-management.md)) and derived the rule
  that makes "integrate-not-build" operational: **adopt components, decline
  peers** (`05-positioning.md`). Full read:
  [`docs/plans/EXTERNAL-ASSESSMENT-2026-07.md`](../docs/plans/EXTERNAL-ASSESSMENT-2026-07.md).
  **Open contradiction surfaced:** the verified subscription-billing finding below
  is still being sold as differentiator #1 in `docs/plans/MARKET-ANALYSIS.md` and
  in the README opening. Needs an owner's call, not a silent edit.

## Verified vs assumed register

| Claim | V/A | Source · date |
|---|---|---|
| n8n is fair-code (Sustainable Use License); hosting/reselling a product that substantially depends on n8n is prohibited without a commercial agreement | **Verified** | [n8n docs](https://docs.n8n.io/sustainable-use-license/), [blog](https://blog.n8n.io/announcing-new-sustainable-use-license/) · 2026-07 |
| LiteLLM is MIT; OSS build includes multi-provider routing, per-key/user **budget** caps, rate limits, Docker | **Verified** | [TrueFoundry](https://www.truefoundry.com/blog/litellm-pricing-guide) · 2026-07 |
| Langfuse core is MIT (open-core); enterprise audit-logging/retention behind a commercial licence when self-hosted | **Verified** | [Langfuse](https://langfuse.com/docs/open-source) · 2026-07 |
| "Keel" is a live **agent-governance compliance middleware that evaluates every tool call against policy before it runs** | **Verified** | [search: keel-ai/PyPI + usekeel.com](https://pypi.org/project/keel-ai/) · 2026-07 |
| Claude automation on subscription auth moved to **metered credit pools (Jun 15 2026)** — flat-subscription no longer covers headless/agent volume | **Verified** | [Verdent](https://www.verdent.ai/guides/claude-code-pricing-2026), [morphllm](https://www.morphllm.com/ai-coding-costs) · 2026-07 |
| EU AI Act high-risk obligations (human oversight, 6-mo log retention) enforce **Aug 2 2026** | **Verified** | [Zylos](https://zylos.ai/research/2026-05-01-ai-agent-governance-compliance-2026/) · 2026-07 |
| Open-source governance projects occupy Brightworks' combination (agentward, Agentic Control Plane, agent-fleet-o, AgenticAudit, open-managed-agents) | **Verified** | GitHub search · 2026-07 |
