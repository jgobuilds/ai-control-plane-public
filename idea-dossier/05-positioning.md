# 05 — Positioning & name

## The name — the evocative space is saturated

Per the naming playbook: *every name that directly expresses a category's core
virtue is already taken.* For "stability / governance / reliability" that is
emphatically true. Every evocative candidate checked (2026-07) is a live product,
several in this exact category:

| Candidate | Collision | Verdict |
|---|---|---|
| **Keel** | [keel-ai](https://pypi.org/project/keel-ai/) + usekeel.com — a live **agent-governance compliance middleware that evaluates every tool call against policy** | **Dead — exact category + name** |
| **Ballast** | [Ballast](https://www.capterra.com/p/186339/Ballast/) risk/compliance software; Ballast Labs; Ballast Lane AI | Dead — live compliance software |
| **Assay** | [assay-ai](https://pypi.org/project/assay-ai/) + [assay.tools](https://assay.tools/about) — LLM eval + signed per-run audit artifacts | Dead — adjacent (audit/eval) |
| **Plumb** | [Plumb](https://theresanaiforthat.com/ai/plumb/) — live AI pipeline/agent builder | Dead |
| **Trellis** | [mindfold-ai/Trellis](https://github.com/mindfold-ai/trellis) "agent harness"; a Trellis that keeps "coding agents to your engineering standards"; +3 AI cos | Dead — in-category, multiple |
| **Keelson** | maritime API, customer platform, PyPI | Taken across software |

"Open Agentic Framework" (the original proposal) also fails: generic/descriptive
(unregistrable, un-SEO-able), initialism **OAF**, and mislabels a governance layer
as a framework.

**Recommendation:** if a rebrand proceeds (fine for an internal/OSS release —
it's cheap), go **invented/coined** — the playbook's holy grail of ownability —
and run a *dedicated* naming pass that clears each candidate across live products,
trademarks, PyPI/npm, domain and handles. Coinage **starting material only,
UNVETTED — do not adopt without full clearance:** *Steadify, Governa, Truekeel,
Statera* (Latin *statera* = a balance/scales). None cleared; offered as seeds, not
a slate. **A distinctive name does not change the product verdict below.**

## Positioning — the honest frame

The market frame that flatters Brightworks ("the governed agent stack") is exactly
the crowded one. The only *defensible* frame is a smaller one:

> **Not** "another agent-governance platform." **Instead:** an opinionated,
> local-first **reference stack** that *integrates* the proven open components
> (LiteLLM for routing/budget, Langfuse for tracing/eval) with approval gates, a
> PII vault, an audit ledger and the **engineering-standard doctrine** baked in —
> so a small team gets a governed agent setup in an afternoon instead of wiring
> five tools.

This is **integrate-not-build**: Brightworks becomes the *assembly + doctrine +
opinion*, not a competitor to its own components. It is a real niche (a "governed
agent distribution"), but a **thin commercial one** — closer to a popular OSS
template than a fundable product. The durable, non-commercial value is the
**doctrine** (the review lenses, ADRs-with-cost, diagnosability, the
notification/observability taxonomies) — which is content, not software, and is
already the most differentiated thing here.

## The boundary — adopt components, decline peers

*(Added 2026-07-25, after assessing Paperclip. This is the rule that makes
"integrate-not-build" operational instead of a slogan.)*

"Integrate, don't rebuild" is right, and taken literally it would have us adopt
Paperclip — MIT, 74.7k ★, and it ships the work model, leases, resumable sessions
and review UI we lack. We declined ([ADR
0010](../docs/decisions/0010-agent-work-management.md)), and the reason is the
boundary this product needs in order to mean anything:

> **Adopt anything that is a *component*. Decline anything that is a *peer*.**
>
> A component owns one layer and exposes it — LiteLLM (routing/budgets), Langfuse
> (tracing/eval), Presidio (PII detection), DBOS (durable execution). Adopting one
> *subtracts* maintenance and leaves the enforcement point where it was.
>
> A peer owns identity, approvals, budgets and secrets end to end — Paperclip,
> Portkey, agent-fleet-o. Adopting one *adds* a second authority over the same
> actions. The question "which system decided this was allowed?" stops having one
> answer, and the audit ledger stops being evidence.

The single enforcement point **is** the product. Everything defensible here —
the tamper-evident ledger, risk × mode, ethical walls, the conformance suite —
depends on there being exactly one place a decision is made. A second control
plane does not add a feature; it removes the property.

That is also the honest limit on the frame: it means the reference stack will
keep lagging the peers on **operator experience** (work surface, review UI,
mobile) for as long as it holds the boundary. That is a real cost, and it is the
right one to pay while the operator count is one — see ADR 0010's trigger, which
re-opens the question at operator two via a *front-of-router* integration rather
than a replacement.

## One-liner (for the reference-stack frame, if pursued)

> *"A governed, local-first agent stack you can stand up in an afternoon —
> routing, budgets, approvals, PII vault and an audit ledger, wired together and
> opinionated by default."*

The one-sentence "not": *not a hosted platform, not a framework for writing
agents, not a company-of-agents work manager, and not a way to dodge metered API
billing.*
