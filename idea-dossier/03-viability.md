# 03 — Viability

## The one differentiator that mattered — and its expiry

Brightworks' genuinely unusual premise was **flat-subscription CLI billing**: run
`claude -p` / `gemini -p` under a $20–$200/mo subscription instead of metered API
tokens, and treat cost as a *notional yardstick*, not spend.

**As of 2026-06-15 this is largely gone for the use case that matters.** Anthropic
moved automation/headless/Agent-SDK usage on subscription auth to a **separate
metered credit pool billed at standard API rates**
([Verdent](https://www.verdent.ai/guides/claude-code-pricing-2026),
[morphllm](https://www.morphllm.com/ai-coding-costs)). The subscription flat rate
now covers *interactive* coding, not the unattended agent volume Brightworks
orchestrates. The README already hedged this ("high-volume n8n flows will hit
caps"); the vendor has now made it explicit. **The load-bearing differentiator
was retired by a pricing change outside anyone's control.**

## Business model — blocked at the structure level

The proposed dbt-Core open-core split (Apache-ish core + supported edition,
trademark moat) collides with two facts:

1. **The core is commoditized.** The router/budget half is LiteLLM (MIT); the
   observability/audit half is Langfuse (MIT, and *already running the exact
   open-core play* — enterprise audit-log/retention behind a commercial licence).
   A new open-core entrant would be selling, against MIT incumbents, a bundle of
   things those incumbents already give away.
2. **n8n's licence blocks the commercial edition** (see `04-clearance.md`). A
   supported product whose value substantially depends on n8n is a prohibited use
   of the Sustainable Use License without an n8n commercial agreement.

## Willingness to pay

The current workaround for "govern my agents" is **free or nearly so** (LiteLLM +
Langfuse, both MIT, self-hosted). Per the WTP heuristic, when the substitute is
free, monetization is ~an order of magnitude harder. Enterprises *will* pay for
governance (EU-AI-Act pressure is real), but they pay **Portkey / Databricks /
established vendors** with SOC2, SSO, support and a multi-tenant control plane —
not a solo-maintained n8n stack.

## Kill criteria (written to be acted on)

- **Any of these true → do not build the commercial product:** the exact
  positioning already ships as a named product ✅ (Keel); the core is MIT-
  commoditized ✅ (LiteLLM, Langfuse); a load-bearing dependency's licence blocks
  commercialization ✅ (n8n). **All three are true.**

## The single load-bearing risk

**Business, not technical.** The technology works. The risk is that there is *no
defensible commercial wedge*: differentiators commoditized, the price advantage
retired, the name and position taken, and the runtime's licence forbidding the
sale. Building more software does not address a market-structure problem.
