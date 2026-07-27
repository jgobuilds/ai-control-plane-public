# Human-awareness observability: notification tiers + a daily digest

**Date:** 2026-07-23 · **Lens:** `ai-standards/references/notification-taxonomy.md`,
`ai-standards/references/diagnosability.md`, `AIOPS.md`

## Considered

How a human stays aware of what autonomous agents and CI are doing, without
watching dashboards and without drowning in noise. The two failure modes to beat
are **alert fatigue** and **silent drift**. Options, by tier and by build scope:

| Option | Cost | Verdict |
| --- | --- | --- |
| **Three-tier taxonomy** (interrupt / digest / on-demand), event routed by *what the human must do* | $0 — doctrine, no runtime | **Adopt** — the missing conceptual layer; makes "should this notify?" a rule |
| **Daily digest**, anomaly-forward, density-adaptive, over the existing ledger | $0 — runs on the existing stack | **Build** — fills the digest tier |
| Post *every* agent action to Slack | $0 in money; **high attention cost** — fatigue | Reject — firehose → fatigue → the one that matters is missed |
| All-green "success feed" digest | $0 in money; **attention cost** — becomes ignored | Reject — becomes wallpaper; the red one gets ignored too |
| A new metrics store / dashboard for the digest | unpriced — new infra to run and maintain | Reject — `eval_metrics.py` already reads the hash-chained ledger, refuses a broken chain, and reports coverage-first; reuse it |
| A second channel-delivery mechanism | $0 licence; **duplicate maintenance cost** | Reject — the channel-abstracted Notify sub-workflow already exists (`{severity,title,message,source}`) |
| **Collaboration observability now** (cross-agent trace IDs, handoff-as-ledger-event, chain-aware breaker) | $0 licence; **build + maintenance ahead of need** | **Defer** — see *Because* |
| GitHub-API CI-health rollup inside the digest | $0 — GitHub API free within quota | Defer — CI failures already interrupt; a one-line honest note covers it in v1 |
| External dead-man's-switch (e.g. healthchecks.io ping) | free tier; **metered above it** | Defer — the weekly heartbeat is the v1 approximation |

## Chose

1. **Adopt the three-tier taxonomy** as a house lens (`notification-taxonomy.md`).
2. **Build the daily digest** as a thin formatter over `eval_metrics.py`
   (`scripts/daily_digest.py` + `n8n-workflows/daily-digest.workflow.json`),
   routed through the existing Notify seam, anomaly-forward, density-adaptive
   (daily when active, one weekly heartbeat when quiet), coverage-first,
   metadata-only, and never-dark.
3. **Same channel as the CI alerts for now**, split-later via a one-line env
   change at the Notify seam — no workflow edit required.
4. **Defer collaboration observability** to a later phase, recorded below.

## Because

**Reuse over rebuild.** Everything the digest needs already exists: the ledger,
`eval_metrics.analyze()` (chain verification, economics, drift, coverage-first),
and the channel-abstracted Notify sub-workflow. The digest is ~200 lines of
formatting plus a scheduled workflow that copies the `eval-drift` shape — "a
column, not a project," exactly what `AIOPS.md` calls for. It stays
showback/visibility, never crossing into the forecasting or chargeback lines that
doc deliberately does not build.

**The digest applies our own lenses to itself.** It is anomaly-forward so it never
becomes green wallpaper; it is density-adaptive with a heartbeat so *silence is
unambiguous* and a *missing* heartbeat means the lane is down; and it **never goes
dark** — a generator failure surfaces as an error post, not a crash and not
silence (`diagnosability.md`). Its run-log contract (never raises + captures the
deciding facts) is pinned by `tests/test_daily_digest.py`.

**Collaboration is deferred because there is nothing to observe yet.** Cross-agent
trace IDs, handoff-as-ledger-event, and a chain-aware breaker
(`maxHandoffDepth` / `maxFanout`) only pay off once agents actually hand off to
each other — which is not running today. Building the plumbing ahead of a real
trace would be speculative surface area (build-vs-adopt). The ledger already
carries per-decision `scope` / `requester` / `verdict` / `usage`; the missing
piece is a `traceId` threading a multi-agent task into one causal story, and that
is best designed against a real collaboration, not guessed at now. Recorded here
so it is a decision, not an omission.

## Status

Implemented and verified (unit + end-to-end against synthetic active / quiet /
tampered ledgers; the embedded n8n Code node passes `node --check` and executes).
The workflow **ships `active: false`** — deployment is a manual dry-run in n8n
(set the Notify workflow id, mount `/audit`), then flip active. Deferred items
above are the next phase when multi-agent collaboration lands.
