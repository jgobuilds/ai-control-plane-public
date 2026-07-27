# 06 — Backlog & reconciliation

## Enhancement backlog (worth stealing, ranked)

1. **Adopt LiteLLM as the router core** rather than maintaining a bespoke one —
   100+ providers, per-key/user budgets, rate limits, MIT. Collapses the most
   expensive maintenance surface. (Integrate, don't rebuild.)
2. **Adopt Langfuse for tracing/eval** — a real trace UI Brightworks will never
   match by hand; MIT core. Feed the audit ledger into it rather than reinvent.
3. **A credit-style budget ledger** (agent-fleet-o) — a running spend/credit
   balance per scope, not just per-decision notional cost.
4. **Framework-agnostic ingestion** — the governance value is bigger if it wraps
   LangGraph/CrewAI/MCP agents, not only CLI runs.

## Research backlog (what we still don't know, ranked)

1. **Cost of an n8n OEM/commercial agreement** — gates the entire commercial path;
   unknown until we ask n8n. (Or: cost of re-architecting off n8n.)
2. **Is there real demand for a "governed agent distribution"** (integrate-not-
   build reference stack), or do teams just assemble LiteLLM+Langfuse themselves?
   Test with 3–5 target users before building toward it.
3. **Licence choice for our own code** (Apache-2.0 vs AGPL vs BSL) — draft ADR
   0007, confirm with counsel. Moot for a commercial product while n8n is core.
4. **Does the *doctrine* have standalone value** — could the review lenses / ADR
   discipline / observability taxonomies be the actual product (a book, a
   standard, a paid course) rather than the software?

5. **A unit of work + execution lease** (Paperclip) — the vet missed this category
   entirely. **Not** by adopting Paperclip: build the task record and lease at the
   router chokepoint ([ADR 0010](../docs/decisions/0010-agent-work-management.md)),
   because a peer control plane makes threat-model C2 permanent.
6. **Durable/resumable execution** (DBOS, Temporal, Hatchet) — genuinely hard,
   genuinely not ours to write. Deferred with a trigger, presumptive choice DBOS
   (MIT, library-in-Postgres rather than another service).

## Reconciliation — what changed

- **`01` differentiating claim:** revised from surviving to **dead** — both the
  "governed/auditable/cost-bounded" clause (commoditized) and the "flat CLI
  subscription" clause (retired by Anthropic Jun 2026).
- **Risk register:** the load-bearing risk moved from *technical/none* to
  **business — no defensible commercial wedge + n8n licence block.**
- **Rename:** proceed only for internal/OSS identity; go coined + cleared; does
  not affect the product verdict.
- **Downgrade:** the *technical* risk is genuinely low — the stack works and is
  well-built. That is not in question; its commercial defensibility is.
- **2026-07-25 — the landscape had a missing cluster.** The vet compared against
  gateways, observability and governance, and never looked at **agent work
  management**, where Paperclip had 74.7k ★ by the time of the assessment. A
  disconfirming pass that misses a whole category is a warning about the pass, not
  just the category — the search was scoped by *our* architecture rather than by
  the operator's problem. Widen the frame on the next revisit.
- **2026-07-25 — "integrate-not-build" needed a boundary.** Taken literally it
  argued *for* adopting Paperclip. The refined rule — **adopt components, decline
  peers** (`05-positioning.md`) — is what makes the recommendation actionable
  rather than a slogan that inverts under pressure.
- **Unreconciled:** the **subscription-billing claim**. `00-index.md` records it as
  Verified-dead (Anthropic moved to metered credit pools, Jun 15 2026) and this
  file reconciles the differentiating claim to dead — yet
  `docs/plans/MARKET-ANALYSIS.md` still leads with it as differentiator #1 and the
  README opens on it. **The dossier's own conclusion never propagated to the
  outward-facing docs.** Needs an owner's call and one correcting pass.

## Net recommendation

**Do not pursue the open-core commercial product as framed.** Keep Brightworks as
(a) the owner's internal governance harness — where it already earns its keep —
and optionally (b) a **source-available** release for others to self-host, if the
n8n-licence and code-licence questions are settled and the name is coined+cleared.
If a commercial ambition persists, the least-bad path is **the doctrine, not the
platform**: package the standards/lenses/taxonomies (content), which is the one
asset here that is genuinely differentiated and carries no n8n-licence problem.
