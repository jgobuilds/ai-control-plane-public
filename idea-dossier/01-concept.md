# 01 — Concept

## What it is

A **governance, reliability and cost-control layer that sits around** agents run
through vendor CLIs (`claude -p`, `gemini -p`), not a framework for *writing*
agents. Built and running: a policy **router** (deterministic-first → cheapest
capable tier → hard cost ceiling); a **hash-chained audit ledger**; **PII
tokenization** with a vault the runners never see; **human approval gates**;
per-scope **isolation**; a **kill-switch / circuit breaker**; scheduled **lanes**
(digest, eval/drift, retention) that measure agentic-leverage + quality from the
ledger; a notification taxonomy; and engineering-standard review lenses. Runs on
**n8n + Docker**, local-first, no SaaS dependency.

## Who for

Originally: the owner's own multi-project automation. Proposed: teams wanting a
self-hosted, governed way to run agents without metered-API surprise or an
ungoverned tool sprawl.

## The differentiating claim (one falsifiable sentence)

> *"An open, local-first control plane that makes agentic work governed,
> auditable and cost-bounded — uniquely by running on flat CLI subscriptions
> rather than metered API tokens."*

Job 2 attacks this. **Result: it does not survive.** Every clause is either
commoditized (governed/auditable/cost-bounded → LiteLLM + Langfuse + OSS
governance projects), or false as of 2026-07 (the flat-subscription clause — see
`04-clearance.md`: Anthropic metered automation on subscription auth in Jun 2026).

## What this is *not*

- Not an agent-authoring framework (LangGraph / CrewAI / AutoGen / OpenAI Agents
  SDK / Google ADK own that — deliberately not competed with).
- Not a hosted SaaS. Local-first is a genuine constraint and a differentiator vs
  the cloud-first observability vendors — but not a unique one (Langfuse, LiteLLM,
  agentward all self-host).

## Hard constraints

- **n8n dependency** is load-bearing and its licence (fair-code) constrains any
  commercial packaging — see `04-clearance.md`. This is the single most important
  constraint and it was not on the radar when the rebrand was proposed.
