# Build the task record and execution lease into the router; do not adopt Paperclip or a durable-execution engine yet

**Date:** 2026-07-25 · **Lens:** `ai-standards/references/build-vs-adopt.md`,
`cost-awareness.md`, `dependency-security.md`,
`concurrency-and-branching.md`, `change-safety.md`

Resolves **G10** in [GAP-CLOSURE-PLAN.md](../plans/GAP-CLOSURE-PLAN.md) and the
**E4** market row.

## Framing — "agent work management" is four separable needs

Treating it as one product is what makes the answer look like "adopt a platform."

| # | Need | Hard? | Who solves it |
|---|---|---|---|
| **N1** | **Task record** — identity, scope, goal ancestry, assignee, state, thread | No. A schema and CRUD | anyone, incl. us |
| **N2** | **Atomic execution lease** — claim-or-refuse in one transaction, TTL, release | Somewhat. Needs one real atomic primitive | anyone with a transactional store |
| **N3** | **Resumable/durable execution** — survive a crash and continue **without repeating tool calls** | **Yes. Genuinely hard** | Temporal, DBOS, Restate, Hatchet |
| **N4** | **Human review surface** — see work, approve, intervene | No, but tedious | Paperclip, any ticket system |

Only **N3** is hard enough that building it is a mistake. N1/N2/N4 are ordinary.

## Considered

Facts came from the GitHub API (`gh api repos/…`, 2026-07-25), from reading
Paperclip's README at `master`, and from probing this repo — not from comparison
write-ups. Consumer-facing "Paperclip alternatives" listicles were checked and
**discarded**: they compare agent *frameworks* (CrewAI, OpenClaw, LangGraph,
Hermes), which are a different plane, and most are affiliate content.

### Two facts about this stack that decide most of it

1. **There is no database in the stack. At all.** `docker-compose.yml` declares
   four volumes — `claude-config`, `gemini-config`, `n8n-data`, `agentsview-data`
   — and no Postgres, MySQL or Redis. State is files: `audit/decisions.jsonl`,
   `vault/*.json`, `scopes.json`, `policy.json`, plus n8n's own SQLite. Every
   adopt option below introduces a stateful tier we do not currently operate,
   back up, or patch.
2. **No scheduled lane has ever fired** ([ADR
   0009](0009-where-the-control-plane-runs.md)). We do not yet have a backlog of
   untracked autonomous work. **We have the gap, not yet the pain.**

### Options

| Option | Cost | Verdict |
|---|---|---|
| **A. Build N1+N2 in the router**; defer N3; keep n8n + generated HTML for N4 | **$0 licence, $0 infra.** Non-dollar: ~a day of work, and we own a lease implementation forever (small — a claim table with one atomic write). No new service, no new datastore, no new backup surface | **Adopt** — it is the only option that does not add a second authority over agent execution, which is the exact thing C2 is open on |
| **B. Adopt Paperclip wholesale** (MIT, 74.7k ★, created 2026-03) | $0 licence. Non-dollar: **high** — Postgres + a Node/React app + its migrations; 4,945 open issues / **2,919 open PRs** to track; telemetry default-on must be disabled per deploy; and the cost of *leaving* is high once org/task data lives in it | **Reject** — see *Because*. It is a control plane, and we already have one |
| **C. Paperclip in front, as a client of `/route`** — its HTTP/webhook adapter posts to the router, which keeps risk, PII, ledger, killswitch, tiering, RBAC | $0 licence; same infra cost as B. Non-dollar: **duplicate authority** — Paperclip's own budgets, approvals, secrets and identity overlap ours and must be deliberately disabled; we would run a large app for ~a quarter of its features while inheriting 100 % of its maintenance surface | **Defer — the leading adopt option if the trigger fires.** Architecturally sound (the chokepoint survives), and honestly better than B |
| **D. Temporal** (MIT, ★21.8k, 2019) | $0 licence; **fixed infra** — its own cluster + persistence, or Temporal Cloud (metered). Non-dollar: real operational skill; a second scheduler alongside n8n | Defer for **N3 only** — over-built for N1/N2, and the ADR 0009 platform cannot host a cluster |
| **E. DBOS** (MIT, ★1.5k, 2024, 7 open issues) | $0 licence; **needs Postgres** — but *only* Postgres, as a library in-process rather than a service | **Defer — the presumptive N3 choice**, precisely because it collapses the workflow state into one transactional boundary instead of adding a service. Blocked on us having a Postgres at all |
| **F. Hatchet** (MIT, ★7.6k) / **Restate** (★4.2k, `NOASSERTION` licence) | $0–metered; a service each. Restate's licence resolves as non-standard on the GitHub API and **would need clearing before use** | Defer — Hatchet is the middle option; Restate needs a licence read first (`vet-idea` clearance step) |
| **G. n8n Data Tables** as the store (available self-hosted ≥ 1.113.1; we run **2.30.7**) | **$0** — already in the stack, no new dependency | **Adopt as the store for A *if* it can do an atomic conditional update.** **Unverified** — the documented use cases include queue management and dedup, but compare-and-set semantics are not confirmed. If it cannot, fall back to a file lease with the ledger's own discipline |
| **H. Bring-your-own ticket system** (GitHub Issues / Linear / Jira) | Free tier → per-seat metered. Non-dollar: **egress of client-linked work metadata to a third party** | **Reject for client work** — contradicts the no-external-processor positioning that is the whole niche. Viable for the engine's own dogfooding only |

## Chose

1. **Build N1 (task record) and N2 (execution lease) into the router.** The
   router already owns scope resolution, risk scoring, RBAC and the ledger. A
   lease *is* a decision; decisions live at the chokepoint.
2. **Store them in n8n Data Tables if — and only if — an atomic conditional
   update is demonstrable.** Verify with a concurrent-claim spike before
   committing. Otherwise a file-based lease using the same append-and-verify
   discipline as `audit/decisions.jsonl`.
3. **Do not build N3.** Durable replay-without-repeating-tool-calls is exactly
   the "solved problem" `build-vs-adopt` says not to re-implement. Defer with a
   named trigger and a named presumptive choice (DBOS).
4. **Do not adopt Paperclip now.** Re-evaluate as **option C**, never option B.
5. **N4 stays as-is** — n8n's execution list plus the generated HTML views — until
   there is a second operator.

## Because

**The disqualifier for Paperclip is architectural, not circumstantial.** Its own
README says it plainly: *"Paperclip is a full control plane, not a wrapper."*
Adopting it means running two control planes with overlapping and partly
conflicting authority over identity, approvals, budgets and secrets. Threat-model
**C2** is open precisely because *"the router is not the single entry point"* —
and the remedy is to force all traffic through one enforcement point. Introducing
a second control plane while closing C2 makes the open finding permanent. The
telemetry default and the 2,919-open-PR maintenance surface are real objections,
but they are the ones that would go away if the project matured. This one does
not.

**And we do not yet have the problem.** Paperclip's pitch is twenty untracked
Claude Code tabs. Per ADR 0009, **not one scheduled lane in this stack has ever
executed autonomously.** There is no untracked backlog to manage. Adopting a
platform for a pain that has not arrived is the *other* failure `build-vs-adopt`
names — dependency bloat, not NIH — and it would be paid for in an operational
tier (Postgres, migrations, backups) that ADR 0009 has just established this
platform cannot reliably host.

**What this gives up, stated plainly:** if the goal were "run twenty agents on
real work next month," Paperclip gets there in a day and this decision does not.
We are trading time-to-capability for a single enforcement point and a stack with
no database. That trade is right *while the operator count is one and the lanes
are not yet running*, and it stops being right the moment either changes — which
is what the trigger below encodes. We are also accepting that our N4 (a human
looking at n8n's execution list) is genuinely worse than Paperclip's UI. That is
tolerable for one operator and not for two.

**On N3, building would be the real error.** Checkpoint/replay that does not
re-fire tool calls is subtle, and getting it wrong means an agent re-sending an
email on recovery. When we need it, adopt — do not write it.

## Status

**Decided, not built.** Verified: no datastore in the stack (`docker-compose.yml`
volumes list); router is 579 lines and already the policy chokepoint;
`grep -ri ticket` → 1 hit, confirming no existing work model; candidate
maturity/licence figures pulled from the GitHub API on 2026-07-25.

**Assumed, not verified:** that n8n Data Tables supports an atomic conditional
update. That is the first spike and it gates choice 2.

**Revisit when — any one of these:**

- **A second operator** needs to see or approve work → re-evaluate **option C**
  (Paperclip in front of `/route`), not option B.
- **Long-horizon tasks** appear that must survive a runner crash mid-tool-call →
  adopt **DBOS** for N3 (and only then does a Postgres enter the stack).
- **The always-on host from ADR 0009 option B lands** → the infra objection to
  every adopt option weakens materially; re-read this table.
- **Paperclip's open-PR count falls below ~500 and telemetry defaults to off** →
  the dependency-security objection clears, though the two-control-planes
  objection still stands.
