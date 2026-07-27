# The scheduled Python lanes run host-side, not inside n8n

**Date:** 2026-07-23 · **Lens:** `ai-standards/references/change-safety.md`
(verify from source), `ai-standards/references/diagnosability.md`

## Considered

Three workflows (`daily-digest`, `eval-drift`, `retention-sweep`) were authored with
`executeCommand` nodes running `cd /app && python scripts/<name>.py`. That assumes an
n8n container provisioned with Python, the repo, and the audit ledger. It isn't.

| Option | Cost | Verdict |
| --- | --- | --- |
| `executeCommand` runs python **inside n8n** (as authored) | n/a — impossible | **Impossible.** The container has no python, no `/app`, no `/audit`. |
| Add python to the n8n image (`apk add python3`) | n/a — impossible (no package manager) | **Impossible.** `n8nio/n8n` is a **Docker Hardened Image** (Alpine 3.24 with the package manager *removed* by design) — no `apk`, `apt-get`, `dnf`. Verified by probing the image directly. |
| Run the lanes in another existing service | n/a — no service has python | **Not available.** Probed every container — router, scrubber, claude-runner, gemini-runner, agentsview: **none has python**. The whole stack is node. |
| Copy python in via a multi-stage build | $0 in money; **security cost** — subverts a hardened base | **Rejected.** Technically possible (same musl/Alpine), but it deliberately subverts a security-hardened base image — a bad trade inside a *governance* framework. |
| **Run the lanes host-side** (scheduler on the host, or a dedicated python sidecar) | $0 | **Adopt.** |

## Chose

The Python lanes are **host-side tooling**. Schedule them on the host (Task
Scheduler / cron), or in a purpose-built python sidecar if they must live in the
stack. Do **not** try to make n8n run python.

## Because

Every Python script here (`eval_metrics.py`, `daily_digest.py`, `conformance_test.py`,
`retention_sweep.py`) already runs correctly on the host against the same ledger —
that is how they have always been exercised. The container fleet is intentionally
node-only and hardened; bending it to host python buys nothing and costs the
hardened posture.

n8n remains valuable as the **orchestration + notification seam** for lanes that are
HTTP- or node-shaped. A python lane that must be n8n-driven should expose an HTTP
endpoint that n8n calls, rather than n8n shelling out to python.

## Status — RESOLVED via the HTTP path (option (a)), everything stays in n8n

The operator's requirement was that lanes stay "managed and logged through our common
infra", so we took the HTTP option named above rather than moving work to the host:

- **New `lanes` service** (`lanes/`): python:3.12-alpine, stdlib `http.server`, mounts
  `./scripts` and `./audit` **read-only**, no published host port (internal to
  `agentnet` only), fail-closed on `LANES_TOKEN`. Endpoints: `/health`,
  `/digest?days=N`, `/eval-metrics?days=N`.
- **`daily-digest` rewired**: the impossible `executeCommand` became an
  `httpRequest` to `http://lanes:8081/digest?days=1`. **n8n keeps the schedule, the
  execution record, the IF gate and the Notify seam** — only the python interpreter
  moved.
- Verified hop by hop: n8n→lanes reachable and fail-closed (401 without the token),
  `/digest` returns correct payloads against the real ledger for both a quiet and an
  active window, the Parse node passes `node --check` and executes, and a POST from
  **inside the n8n container** to the Slack webhook returned `ok`.
- **The lane is ACTIVE**, deployed entirely via `scripts/n8n_bootstrap.py` — no UI clicks.

`eval-drift` and `retention-sweep` can be unblocked the same way by pointing them at
`lanes` endpoints (they will need `./context` and `./router` mounted read-only too).

**Known limitation:** `n8n execute --id` (the bootstrap's `--dry-run`) cannot run
against an already-running instance — it fails with "Task Broker's port 5679 is already
in use". Use a temporary schedule or the REST API for an on-demand run.

**Found the honest way:** by probing the container before activating. Had the lane
been activated on the strength of the authored command, it would have failed silently
on its first 07:30 schedule.

## Addendum, 2026-07-26 — the hardened image cannot WRITE FILES either

This ADR recorded that `n8nio/n8n` ships no package manager and no python. It
also cannot write files, and that is a stronger constraint than it sounds.

Found while building the ask-human handshake, which needed somewhere to hold an
answer between two requests. n8n's **Read/Write Files** node fails every write:

```
The file "/asks/isolation-test.json" is not writable.
The file "/tmp/n8n-write-probe.json" is not writable.
```

Three explanations were tested and **all three were wrong**, which is why they
are written down — each is the obvious guess and each wastes a cycle:

1. *WSL/drvfs bind mount* (the scrubber's `chown` no-op is a real instance of
   this). Disproved: a **named volume** fails identically.
2. *Directory ownership.* Disproved: `chown 1000:1000` on the volume, so the
   `node` user owned it, changed nothing.
3. *That path is special.* Disproved: `/tmp` fails too — a path the container
   unquestionably owns, where the shell writes fine as the same user.

The write operation is disabled in the image. A shell inside the container
writes without complaint; the node refuses regardless of path or permissions.

**Consequence for future designs: n8n can orchestrate, schedule, gate and call —
it cannot persist.** Anything needing state between two executions must put it
somewhere else: a service we control (`lanes`, `router`, `scrubber`), n8n's own
workflow static data, or a database. Designs that assume n8n can drop a file are
dead on arrival, and now they are dead in writing rather than in someone's
afternoon.

### Correction, same day — one of the three "constraints" was my own bug

The addendum above stands: n8n genuinely cannot write files, and it genuinely
does not await a child suspended on a Wait node. A **third** blocker recorded
elsewhere — "n8n will not register a second webhook" — was **wrong**.

The real cause: `webhookId` is **required** on a webhook node and must be a
**UUID**. Given a human-readable string, n8n accepts the import, activates the
workflow, and then fails the request with:

```
Cannot read properties of undefined (reading 'node')
```

An internal error naming nothing useful. With a hand-written id the *second*
webhook silently never registers; remove the id entirely and the *first* one
starts 500ing. Two different symptoms, one malformed field.

Worth recording because of how it read from the outside: three unrelated platform
limits in a row, which is a story that invites the conclusion "wrong platform".
Two were real. The third was mine, and an error message that named the field
instead of dereferencing undefined would have cost minutes rather than a
transport rewrite.

**Method note:** the two real constraints were each confirmed by an isolation
test (write to `/tmp`; time the webhook). The false one was concluded from
repeated failure without one. Isolate before you generalise.
