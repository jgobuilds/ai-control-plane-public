# Status view — one page, generated from the live stack

`python scripts/gen_status.py` writes `status.html`: gates, containers, lanes,
the audit ledger and agent-session analytics on one page. It is deploy-local
evidence, not source — the generator is committed, the output is gitignored,
because the page names real scopes and project paths.

```bash
python scripts/gen_status.py              # write status.html
python scripts/gen_status.py --open       # write it and open it
python scripts/status_data.py --no-gates  # the raw JSON, without the slow half
```

`gen_status.py` **exits 1 if any gate fails**, so it doubles as a single command
that answers "is this stack healthy" from a shell or a lane.

## What it shows, and where each number comes from

| Section | Source | Why this source |
|---|---|---|
| Gates | the existing check scripts, by **exit code** | reuses the gates rather than reimplementing their logic and drifting from it |
| Containers | `docker ps -a` | what is actually running, not what compose declares |
| Lanes | `lanes/schedule.py` `SCHEDULES` vs the scheduler state file | declared schedule against last-run reality |
| Ledger | `audit/decisions.jsonl` | the router's own hash-chained record |
| Sessions | Claude Code transcripts in the `claude-config` volume | usage without a third-party collector |

Nothing here is a new source of truth. Two design rules hold it to that:

- **A gate that cannot run is not a pass.** `unknown` and `skipped` are distinct
  states from `pass`, in their own colour. Counting "didn't run" as green is the
  vacuity this repo keeps catching in its own controls.
- **No section can break the page.** Each carries its own `error` instead of
  raising — a status page whose job is to report trouble must not fail when
  something is broken.

## Block reasons are normalised, deliberately

The router writes clean values for policy blocks (`halted`, `approval-required`)
and `error:<message>` for faults. Bucketing on the raw message gives fifteen
categories of one instead of "runner error: 15" — and it once put a **prompt** on
screen, which is finding **D3** in [THREAT-MODEL.md](../security/THREAT-MODEL.md).
`status_data.block_category()` maps messages to stable labels; `tests/status_data_test.py`
pins that mapping, including that a prompt-shaped message never becomes a category.

## It replaced a container

`agentsview` (a third-party image) mounted the same session volume and showed the
usage half — sessions, messages, projects, active days, a heatmap. It had been
reading **0 of 71** session files for weeks with nobody noticing, which is the
argument for owning ~80 lines instead of running a container for them. Dropping it
also removed a 276 MB image, a named volume and a pinned external digest from
`context/image-policy.json`. The removal is recorded in
[ADR 0010](../decisions/0010-agent-work-management.md)'s successors and the
`agentsview` mentions left in `docs/decisions/` are historical records, kept.

Reading that volume still needs a container, because it is a named docker volume
rather than a host path: `docker run --rm` against an image already built here, so
it works whether or not any service is up.
