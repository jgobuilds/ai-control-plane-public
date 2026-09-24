# Recurrence register — seen once, fix it; seen twice, fix the harness

The repo already had three good habits for a failure: fix it, add a known-cause
row so the next diagnosis is free, write a solution while the context is fresh.
None of them **counted**. Nothing added up how many times the same thing had
happened, so "this keeps failing" stayed an impression.

The impression was right. Dependabot's docker updater aborted every day for five
days on a directory containing no Dockerfile, and nobody escalated, because on
any given morning it was one red run. The cost was not the red X:
dependency-security has a scanner half that *tells* you a dependency is
vulnerable and a Dependabot half that *opens the PR* fixing it. The scanner kept
reporting, so the repo looked covered while the fixing half was dead.

The register makes the count a fact and attaches a rule to it:

| Occurrence | What it costs you |
|---|---|
| **1st** | Fix the instance. No paperwork. Not every failure is a pattern, and a register that demands an entry for every red run is a register nobody fills in. |
| **2nd** | Fix the **harness**, and record it. Twice is a pattern, and a pattern only a human notices gets noticed later and later. |

```bash
python scripts/recurrence.py collect       # append what happened
python scripts/recurrence.py report        # what is recurring
python scripts/recurrence.py check         # the gate (counts + honesty)
python scripts/recurrence.py check --shape # the gate CI can actually run
```

## The three files

| | |
|---|---|
| `scripts/recurrence.py` | collector, gate, report |
| [`reliability/recurrence.yaml`](../../reliability/recurrence.yaml) | the register — **tracked**, one entry per pattern |
| `audit/recurrence.jsonl` | occurrences — append-only, **gitignored** runtime evidence |

## Dispositions, and what each one costs

A signature seen twice with no entry fails the gate. So does an entry that lies.

- **`harness-fixed`** — must carry `asserted_by`: a command plus an expected
  substring, which the gate runs. Lifted from `security/findings.yaml` for the
  same reason it exists there: *a claim nobody runs is not a verified claim.* A
  suite that still exits zero after someone deletes the one check that mattered
  satisfies "exit 0" and fails this. Add `requires` when a proof needs a runtime
  the machine may not have (node lives only in the containers here) — that
  reports UNVERIFIED locally and fails hard in CI.
- **`watching`** / **`accepted`** — must carry `why` **and `until`**.

`until` is the only thing keeping the file honest. A disposition with no expiry
is how a register becomes a graveyard of things somebody once decided to live
with. When it passes, the gate fails and a human re-decides. Re-deciding "still
fine, +90 days" is a fine outcome; never being asked is not.

## Two check modes, on purpose

Occurrences live in `audit/`, which is gitignored — so **in CI there is no
occurrence log at all**, and a counting gate there would pass while counting
nothing. That is the vacuity this repo keeps catching in its own controls, so
the gate is split:

- `check --shape` runs in CI: the register's own honesty. Every claimed harness
  fix's proof is executed; nothing expired.
- `check` runs where the data is — the deploy host, and as a gate on the
  [status page](STATUS-VIEW.md). With no occurrence log it reports **UNKNOWN**,
  never pass.

## Signatures

Two runs of one failure must produce one signature or nothing ever reaches two.

1. **A known cause wins.** `scripts/ci_diagnose.py`'s tables are matched first,
   giving the stable id `ci:cause:<id>` instead of a line that shifts the moment
   someone rewords an error message. That is also where a diagnosed failure is
   meant to be promoted to, so the two tables reinforce each other.
2. **Otherwise, the strongest failing line, normalised** — timestamps, container
   ids, job numbers and counts stripped, because that is precisely what makes two
   identical failures look like two different ones. Line ranking is
   `ci_diagnose.failing_lines`, reused rather than rewritten: it already lost the
   fight a fresh regex would lose, since this repo's suites print lines like
   `PASS  a PASSING security check is not diagnosed as a failure`.

## A refusal is not a fault

The first version of the router source counted `unknown scope`, `retired scope`
and `cross-vendor blocked` as recurring incidents — ten of them — when every one
was the router **correctly refusing a call**. A register that escalates the
control working demands a "harness fix" for behaviour that is already right, and
that is how a gate earns a reputation for crying wolf in its first week.

The dividing question is not "was the request stopped" but **"did the system do
its job"**. `POLICY_REFUSALS` in `scripts/recurrence.py` is that line, and
`tests/recurrence_test.py` pins it.

## Sources

| Source | Reads | Needs |
|---|---|---|
| `ci` | failed GitHub Actions runs | `gh` (found off-PATH on Windows too) |
| `router` | non-policy blocks in the audit ledger, bucketed by the same `block_category` the status page uses | the ledger |
| `lane` | n8n executions with status `error` | `N8N_API_KEY` |

A source that cannot be read reports **UNAVAILABLE** by name. A collector that
silently contributes zero is indistinguishable from a quiet week.

```bash
python scripts/recurrence.py sources   # can each one be read RIGHT NOW, and why not
```

"The credential is in place" and "the credential works" are different claims,
and the gap between them is invisible in a report — an unavailable source and a
quiet one both contribute nothing. `sources` answers it directly and writes
nothing, so it is the safe thing to run after pasting a key.

### The `lane` source, specifically

It needs an n8n **public API key**, which is UI-only: `n8n --help` lists no
api-key command in 2.30.7, so there is no scriptable path. Create it at
**Settings → n8n API → Create an API key** and put it in `.env` as
`N8N_API_KEY`. Leave `N8N_API_URL` blank — the commented `http://n8n:5678/...`
resolves only inside the compose network, and the host-side collect needs
localhost, which is its default when empty.

`recurrence.py` reads those two names out of `.env` when the environment does not
already have them, because the daily collect runs from the Task Scheduler with a
bare environment — otherwise the key could sit on disk forever while the source
kept reporting UNAVAILABLE. Narrow on purpose: only those names, only when unset,
values never printed.

Two things the API spec settled, checked against `/api/v1/openapi.yml` on the
running instance rather than against memory:

- **Signatures key on `workflowId`, not the workflow name.** The first version
  read `execution.workflowData.name`, a field the schema does not have, so every
  signature would have fallen through to the id in silence. The id is the better
  key regardless — ids here are stable (`aicp-*`), while a name is display text
  somebody renames, and a rename would split one recurrence into two. The name
  rides along as detail.
- **A 401 is reported as a rejected key, not an outage.** They need different
  fixes, and calling a bad credential "unreachable" sends you to the wrong place
  for as long as you believe it.

## The review lane

`n8n-workflows/recurrence-review.workflow.json` runs **weekly, Monday 08:00**:
schedule trigger → `GET lanes:8081/recurrence?days=90` → format → the
channel-abstracted **Notify** sub-workflow. It also carries a *Run on demand*
trigger, which is the only way to exercise a scheduled lane.

**The endpoint is read-only by construction**, the same way `/retention` is
dry-run by construction. It never calls `collect`, and no parameter could make
it: `/audit` is mounted read-only into the lane on purpose, and the register is a
tracked file that a scheduled lane has no business editing — a lane that could
rewrite a disposition could close its own findings.

That leaves a split worth stating plainly:

| | Where it runs | Why |
|---|---|---|
| `collect` | the host, **daily 06:15** | needs `gh` plus a credential, and write access |
| report / notify | the lane, **Mon 08:00** | needs neither, and `build(live=True)` re-derives the router source from the mounted ledger in memory |

```powershell
powershell -File scripts\schedule_collect.ps1 -Install     # register the daily task
powershell -File scripts\schedule_collect.ps1 -Status      # state, last result, log tail
powershell -File scripts\schedule_collect.ps1 -RunNow      # fire it through the scheduler
powershell -File scripts\schedule_collect.ps1 -ShowCron    # the POSIX one-liner
```

The task runs `scripts/collect_recurrence.cmd`, which appends every outcome —
including failures — to `audit/recurrence-collect.log` and returns the collect's
exit code, so a failing collector shows as a failing task rather than a green one
that wrote a sad line into a file nobody opens.

**What watches the watcher.** If the task dies the occurrence log simply stops
growing, and a stopped collector looks exactly like a quiet fortnight. So a
source silent for more than `STALE_DAYS` (14) is reported as
`STALE — ... has collect stopped?`, and the weekly lane escalates that word to a
`warn`. Never-collected and stopped-collecting are separate messages, because
they need different fixes. Both are pinned by tests, on both sides.

So the **router** half of the weekly message is always current, and the **CI**
half is only as fresh as the last host-side `collect`. The message says which,
per source, because *no CI failures* and *nobody has collected since April*
produce the identical empty list and only one of them is good news. A blind
source makes the whole message a `warn`.

The lane raises three separate alarms, deliberately not merged: **harness debt**
(seen twice, undispositioned), **a disposition expiring** (said before it turns
into a red gate — a prompt, not an ambush), and **a blind source**.

### Two things the wiring taught

- **The lanes image was stdlib-only, and that broke the endpoint silently.**
  Without pyyaml the register could not be parsed, so every signature came back
  `undispositioned` — the lane would have reported "4 patterns need a harness fix"
  every Monday about four patterns that are all dispositioned. Found by *calling*
  the endpoint rather than assuming it worked. `lanes/requirements.txt` carries
  the one pin and the three alternatives that lost.
- **A CLI activation does not register a schedule with the running n8n.** Known
  here since 2026-07-27 (`docs/solutions/n8n-schedule-not-registered-after-cli-activation.md`);
  the workflow reads as active from every angle a person can check while the
  scheduler has never heard of it. `docker compose restart n8n` after activating,
  then confirm n8n's own startup log says `Activated workflow ... (ID: aicp-recurrence-review)`.

`tests/unit/recurrence-lane.test.mjs` runs the Code node's **committed jsCode**
— loaded out of the workflow JSON, so there is only one copy — against a captured
payload: a failed lane call must not read as healthy, a blind source must not read
as quiet, an unreadable register must not read as empty.

## Tests

`tests/recurrence_test.py` synthesises every failure rather than hoping the repo
supplies one — an undispositioned pattern, a proof that exits zero without
proving anything, an expired disposition, a single occurrence that must *not*
fail, a policy refusal that must not even be counted, and a missing log that must
report UNKNOWN. The register passes on the real repo today, which on its own
proves nothing; an empty check passes too.
