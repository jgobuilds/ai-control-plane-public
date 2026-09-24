# AIOps — cost, benefit, and making it routine

The framework had strong cost *governance* and no cost *measurement*. Tiering,
ceilings, and a kill switch decided what to spend; nothing recorded what was
spent. This closes that, and states plainly what the numbers do and don't mean.

## Read this before putting any number on a dashboard

**`notionalCostUsd` is not money.** The runners authenticate on a
**subscription**, not metered API billing. The figure the CLI returns is what
the work *would* have cost at published API rates.

It is recorded anyway, because it is the only **common yardstick** across tiers,
providers, and effort settings — and comparability is exactly what a routing
decision needs. A t3 request that costs 400× a t1 request is a fact worth
knowing whether or not an invoice follows.

Rules:

- Label it **notional** everywhere it surfaces. Never "spend", never "cost" bare.
- Never sum it into a finance report.
- If a lane ever moves to metered billing, the number becomes real — and the
  decision record in `docs/decisions/0001-context-compression.md` should be
  revisited, because that inverts several trade-offs at once.

## What is measured now

The runner already received this and discarded it; the router now records it.

| Field | Meaning |
|---|---|
| `notionalCostUsd` | Executor **+ verifier**. A cross-vendor verify is real extra work; hiding it understates the true price of a t3 request. |
| `inputTokens` / `outputTokens` | As reported by the CLI. |
| `totalInputTokens` | Input **including** cache reads/writes — the number that reflects context-window pressure. `inputTokens` alone excludes the cached remainder. |
| `turns` | Agent turns consumed. |
| `durationMs` | CLI-reported when present, wall time otherwise. Latency is never simply absent. |

Written to the audit ledger per decision. Scope and requester are already on
every record, so **showback falls out for free** — no extra plumbing.

Deterministic (t0) decisions record an explicit **zero**, not a null. That t0
costs nothing is the entire argument for t0, and it should be visible.

## The headline metric: cost per *completed* task

`scripts/eval_metrics.py` reports `notional_cost_per_completed_usd`.

Per-call cost makes the wrong choice look right. A higher `effort` setting costs
more per call and frequently **less per finished task**, because it cuts the
number of turns. Only the per-completed number can see that.

The denominator is *quality-held* completions, so work that failed the bar
counts as cost without benefit — which is what it was. Expect
per-completed > per-attempt; the gap **is** the waste.

**Coverage is reported first.** A cost total computed from 3% of records is a
lie of omission, so `usage_coverage_pct` precedes every total. Records written
before this existed have no `usage` block and are excluded rather than counted
as free.

## Tier mix is the cost lever, not context length (measured 2026-08-31)

A first pass over the ledger's 33 usage-bearing decisions, prompted by reading
Google's SKILL.state paper and asking whether *our* agent runs suffer the
context-growth problem it solves. They do not — and the measurement pointed
somewhere more useful.

**Context growth is not our problem.** `totalInputTokens / turns` is flat to
mildly decreasing from 1 turn (34,326) to 20 turns (22,085). Linear, not
quadratic: Claude Code manages context internally and `inputTokens` of 2–4
against 35–59k total shows prompt caching carrying nearly all of it.

**Tier is our problem, and it is a 13x lever:**

| tier | model | n | median total input | median notional |
|---|---|---|---|---|
| t2 | claude-sonnet-5 | 15 | 34,996 | **$0.0196** |
| t3 | claude-fable-5 | 15 | 59,278 | **$0.2623** |

An even split between two tiers whose median cost differs by more than 13x. That
is the router doing exactly its job — and it means the single highest-leverage
cost question here is *how often t3 is chosen and whether it needed to be*, not
how many tokens a conversation accumulates.

**Why this matters more than it did in May.** Since 2026-06-15 these tokens meter
against a capped per-user monthly Agent SDK credit at standard API rates
([`PROVIDER-BILLING.md`](PROVIDER-BILLING.md)), and the ceiling is a **stop**, not
a slowdown. The $7.64 notional across the ledger is small, but it is now drawn
from a $20–$200 monthly pool rather than an uncapped subscription allowance. Tier
mix converts directly into how much unattended work the month can hold.

**Not yet built, deliberately.** `eval_metrics.py` reports cost per completed
task but does not trend **tier mix over time**, and no gate fires when t3's share
rises. Before building one, the honest prerequisite is the thing this section is
short of: `n=33` is a small sample, split evenly by construction rather than by
observed demand, and the t3 records may simply be the harder tasks — in which
case a rising t3 share is correct behaviour, not waste. The question a trend would
answer is whether t3 is chosen for tasks a cheaper tier completed successfully
before, and that needs the per-completed denominator above, not a raw share.

**Trigger:** usage coverage past ~100 decisions, or the first month where notional
spend approaches the plan's Agent SDK credit. Then trend tier mix against cost per
*completed* task, and only gate on it if the two diverge.

## Spend-aware circuit breaker

`limits` in `router/policy.json` previously counted only requests. A request
ceiling treats a t1 classify and a t3 `xhigh` architecture run as equal when
they differ by orders of magnitude — it is a rate limiter wearing a budget's
name.

```json
"maxNotionalSpendPerScope": 5.0,
"maxNotionalSpendGlobal": 20.0
```

Same sliding window, same fail-closed refusal (`blocked: "circuit-open"`).
Records with no usage block count as 0 — the breaker stays permissive on old
data rather than tripping on absence, consistent with the kill switch's
availability-wins posture elsewhere.

## Benefit — what we will and won't claim

Most published AI-ROI numbers are fabricated. "Hours saved" is unfalsifiable and
will not appear here.

What the ledger and eval lane can actually support:

| Metric | Reads |
|---|---|
| **Agentic leverage** | Share of attempts that held the quality bar. Already computed; explicitly a proxy. |
| **Cost per completed task** | The efficiency denominator. |
| **Turns per completed task** | Falling turns at constant quality is real improvement. |
| **Rework rate** | Verdict-false share — work paid for and thrown away. |
| **Approval rate** | Human touches per completed task; the automation ceiling. |
| **Cycle time** | `durationMs` to accepted result. |

None of these is business value. Together they are a defensible **efficiency**
picture. `EVAL.md` is already careful to call leverage a proxy rather than a
value claim — keep that discipline; it is what makes the numbers trustworthy.

The honest gap: we cannot measure the counterfactual (what this would have cost
without an agent) and should not pretend to. What we can measure is whether the
same work is getting cheaper, faster, and less often rejected over time.

## Making it routine

A metric nobody looks at is not a control. The pattern that already works in
this repo is a check that **fails** — `model_policy_check.py` fails when its
review date passes. Reuse that; don't create a meeting.

| When | What | Where it hooks |
|---|---|---|
| Every run | cost/tokens/turns/duration to the ledger | automatic, router |
| **Daily** | **what happened + anything wrong** (anomaly-forward digest) | **`daily-digest.workflow.json` → Notify** |
| Weekly | cost-per-completed trend + drift flags | existing scheduled eval lane |
| Per PR | cost delta for the change | CI, beside conformance |
| Quarterly | "is each tier still earning its cost?" | existing `reviewBy` gate in `model-policy.json` |

The daily and weekly rows are complementary, not redundant: **daily** is the
operational heartbeat (today's activity, blocks, drift — density-adaptive, silent
on quiet days bar a weekly heartbeat), **weekly** is the cost-per-completed
*trend* (a trend needs a week; two records are not one). Both flow through the
same channel-abstracted Notify seam. `scripts/daily_digest.py` is a thin formatter
over `eval_metrics.py` — it stays showback/visibility, never crossing into the
forecasting/chargeback lines below. See
`ai-standards/references/notification-taxonomy.md` for the interrupt/digest/
on-demand tiers this fills.

Most of those already exist and need a column, not a project. That is the
point: **instrument once, surface where people already look.**

## Deliberately not built

- **Forecasting.** Two ledger records are not a trend. Revisit at real volume.
- **Chargeback.** Showback (visibility) is free from existing attribution;
  chargeback (billing a team) needs an accounting owner, and notional cost is
  the wrong basis for it.
- **Per-token pricing tables.** `context/model-policy.json` already carries
  prices with a review date. Duplicating them here would drift.

## Verifying

```bash
python scripts/eval_metrics.py --format json     # economics block
python scripts/conformance_test.py               # invariants hold
```

`eval_metrics.py` refuses to compute anything on a broken hash chain — a ledger
it cannot verify is one it will not measure. That refusal is a feature; if it
fires, fix the ledger rather than the threshold.
