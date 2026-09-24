# Replace n8n with a scheduler + durable-wait built into the services we already run — not another orchestrator

**Date:** 2026-07-28 · **Lens:** `ai-standards/references/build-vs-adopt.md`,
`cost-awareness.md`, `change-safety.md`, `concurrency-and-branching.md`

## Considered

Facts probed from the running stack and the workflow JSON on 2026-07-28, not from
memory: `n8n list:workflow`, the `workflow_entity` and `execution_entity` tables
queried directly, and a node census across all 18 workflow files.

**What n8n is actually doing for us** — the requirement, before any option:

- **Schedule triggers — 8.** Daily 06:00 and 07:30 · weekly Mon 06:00, 07:00,
  07:30 · every 10 min · every 15 min ×2.
- **Sub-workflow call + trigger — 15 / 11.** Fan-in to `notify`,
  `approval-gate`, `ask-question`.
- **HTTP request nodes — 26.** Every one points at our own `router` or `lanes`.
- **Code nodes — 38 (~845 lines of JS).** Formatting, triage, classification.
- **Branching — 12** (`if` / `switch` / `set`).
- **Webhook triggers — 3.**
- **Durable `wait` — 2.** ← the only hard requirement.
- **Error-workflow binding — 1**, bound to every lane.

**The two waits are the whole problem.** Everything else is a cron entry, an
HTTP call, or a function.

- `approval-gate`: `resume: webhook`, **24h `limitWaitTime`**, then default-deny.
- `ask-question`: `resume: form` — n8n **hosts an HTML form** for the human. This
  is a UI feature, not just a wait, and it is the piece most likely to be
  under-estimated.

**Dependency depth, measured:** ~10,200 lines of logic live in `router/`,
`scrubber/`, `lanes/` and `scripts/`; ~845 lines live inside n8n Code nodes —
**8%**. The chokepoint doctrine ("n8n calls the router, not the runners") pushed
cost tiering, policy, the ledger, PII and RBAC outside n8n years before the
licence mattered.

| Option | Cost | Verdict |
|---|---|---|
| **A. Keep n8n, buy the commercial licence** | Metered, real money, recurring, before any revenue exists. Non-dollar: the exposure is permanent — a licence that changed once can change again | **Reject** — pays rent forever to keep an 8% dependency |
| **B. Windmill** (AGPL-3.0) | Free self-hosted; real money at the Enterprise tier. Non-dollar: a migration to a second workflow UI, plus **the same class of risk again** — an open-core workflow tool whose licence is set by a vendor | **Reject** — swaps one vendor's licence risk for another's while re-paying the migration cost |
| **C. Temporal** (MIT) | Free software; **an always-on server + workers**, on a stack whose whole deployment story is "one Docker host". Non-dollar: substantial operational surface and a programming model to learn | **Reject** — correct answer for durable sagas; we have two 24-hour waits |
| **D. Prefect** (Apache-2.0) | Free self-hosted. Non-dollar: Python-native and a good fit for the lanes, but **human-in-the-loop pause/resume is not its shape** — we would still build the wait | **Reject** — solves the easy half, leaves the hard half |
| **E. DBOS Transact** (MIT) | Free library; **requires Postgres**, which this stack does not run. Non-dollar: a new datastore to operate, back up and retain | **Defer** — ADR 0010 already deferred here with a trigger; that trigger has not fired |
| **F. Build it into `lanes` + `router`** | Free. Non-dollar: **we own the durable-wait correctness**, which is the real price, plus porting ~845 lines of JS | **Adopt** |
| **G. Hatchet** (MIT) | Free; Postgres again | Reject — same new-datastore cost as E without ADR 0010's prior reasoning |
| **H. Restate / Inngest** | Source-available (BSL-class) | **Reject on licence** — replacing a licence blocker with a licence blocker |

## Chose

1. **Scheduling → a stdlib scheduler inside the existing `lanes` service**
   (`lanes/schedule.py`). APScheduler was the obvious pick and was rejected on
   contact with the code: `lanes/server.py` is pure stdlib and says so, as does
   every shared script here. Eight fixed schedules do not justify the first
   runtime dependency in a service that has none. `build-vs-adopt` cuts both
   ways — a cron table is not the hard part of an orchestrator.
2. **Durable wait → a `pending_actions` table + a sweeper**, owned by the
   router. This is [ADR 0010](0010-agent-work-management.md)'s task record (N1)
   finally built, not a new idea: *"build the task record and execution lease
   into the router."* Resume by signed URL; the sweeper applies the 24h
   **default-deny**, which is the behaviour that must not regress.
3. **The human form → a minimal server-rendered page** on the existing
   approval/ask endpoints. No framework, no JS build.
4. **Sub-workflow calls → direct HTTP calls** to the same endpoints. They are
   already HTTP internally; n8n was a middleman.
5. **Error binding → a decorator in the lane runner** that writes the failure to
   the ledger and calls `notify`, which is what the global handler does.
6. **Port the 38 Code nodes to Python** in `lanes/`, where they become testable.
7. **Cut over lane-by-lane, both systems live**, n8n schedules disabled one at a
   time. No big-bang switch.

## Because

**The 8% figure decides this.** Adopting Temporal, Windmill or Prefect means
taking a large dependency to replace a small one — and every candidate except
DBOS still leaves us building the human-in-the-loop wait, which is the only part
that is genuinely hard. `build-vs-adopt` says survey before building; the survey
says the thing on offer is not the thing we need.

**What we give up, plainly:**

- **The n8n UI.** Execution history, a visual editor, and the retry button all
  go. `agentsview` covers some of it and will need to cover more. This is the
  biggest real loss and it is not a small one — a text log is not an execution
  list.
- **Correctness of the durable wait becomes ours.** Temporal exists because this
  is subtle. Our version is narrow — two waits, one timeout rule, no retries
  across steps, no versioning — but "narrow" is a claim that has to stay true.
  If a third wait wants different semantics, revisit rather than extend.
- **Migration risk on a live system.** Eight schedules that currently fire
  correctly, verified this morning. Cutting over lane-by-lane keeps the blast
  radius to one lane.
- **A visual editor is a real onboarding affordance** for anyone who is not the
  author. Written down because the person deciding this is the author.

**What it buys:** the licence blocker disappears — n8n's Sustainable Use Licence
forbids selling a product whose value substantially depends on n8n, and that is
the single thing standing between this stack and any commercial offering. It
also removes a container, a SQLite database, a credentials store whose
encryption key must not be recreated (ADR 0009), and the class of failure where
CLI activation does not register a schedule until a restart.

## Status

**Increment 1 built and tested; nothing cut over.** All 8 n8n schedules still
run and were verified firing on 2026-07-28 (`aicp-dispatch` 74 runs across
11.4h; `aicp-retention-sweep` at 06:00:52 EDT, 52s after its cron time).

`lanes/schedule.py` exists and defaults to **dry mode** — it computes and logs
and calls nothing, which is what makes it safe to run beside n8n. Cutover is
per-lane via `AICP_SCHED_LIVE_LANES`, because two schedulers firing the same
lane would double-run it, and for the retention sweep that means running a
destructive job twice.

`tests/schedule_test.py` asserts the schedule table **against the n8n workflow
JSON** rather than against itself — 10 parity checks, so a cutover cannot
silently shift a lane by an hour. It also covers the classic scheduler defect
(a boundary that returns the current instant and double-fires) and wall-clock
alignment, so a restart at 06:03 does not shift an every-10 lane to :03 forever.

One deliberate improvement over what it replaces: a firing that was due while
the process was down is emitted as **MISSED** with its due time. n8n neither
backfills nor tells you. Reported, not replayed — running a 06:00 retention
sweep at 14:00 is a different action from running it at 06:00.

**Not yet built:** the durable wait, the human form, the Code-node port, and the
error-handler decorator. Those are increments 2–5.

Increment order, each verified before the next: **(1)** scheduler alongside n8n,
firing into a no-op sink · **(2)** one lane cut over, n8n's copy disabled ·
**(3)** remaining lanes · **(4)** durable wait + form · **(5)** n8n removed from
`docker-compose.yml`.

**Revisit when:** a third durable wait needs semantics the two-wait design does
not cover; or a second operator needs the visual editor; or the workflow count
passes ~30, where a hand-rolled scheduler stops being obviously cheaper than
adopting one.
