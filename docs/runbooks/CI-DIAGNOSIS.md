# CI failure diagnosis

When CI goes red, the alert used to carry a red X and a link. Someone then read
the log and re-derived a cause the project had often already diagnosed once
before. This turns that into a mechanism: deterministic work before expensive work,
applied to our own builds.

```
deterministic  ->  model  ->  human
known cause        hypothesis   decide
```

## Tier 1 — deterministic (runs in CI, costs nothing)

[`scripts/ci_diagnose.py`](../../scripts/ci_diagnose.py) matches the failed log
against two unioned tables. A
hit produces the cause and the confirmed fix, identically every time, with no
model call and no possibility of hallucination.

The `notify` job in [`quality.yml`](../../.github/workflows/quality.yml) runs it
before posting, so the alert carries the cause rather than just the failure.

## Where the diagnosis shows up

The split follows
[`notification-taxonomy`](../../AGENTS.md): **an alert is an interrupt, so it
gets the conclusion and nothing else**; the supporting detail is one click away.
Dumping a full diagnosis into a channel is how a useful alert becomes one people
scroll past.

| Surface | Carries | Needs |
|---|---|---|
| Slack alert | **one BLUF line** — the cause, or "unrecognised failure" | the existing webhook |
| GitHub comment — on the **PR**, or on the **commit** for a push | cause, confirmed fix, redacted failing lines | nothing; the built-in token |
| Run-page job summary | the same, on the run itself | nothing |
| Slack **thread reply** under the alert | the same | `SLACK_BOT_TOKEN` + `SLACK_CHANNEL_ID` |

[`scripts/ci_comment.py`](../../scripts/ci_comment.py) posts all four. It is a
**separate tool from the diagnostician on purpose** — `ci_diagnose.py` has no
network and no subprocess, asserted structurally, and that guarantee only stays
absolute if the thing that reaches the network lives elsewhere. The poster
writes *comments*: it cannot commit, push, edit a file, or rerun a job, and the
test asserts no rerun/dispatch/git-write endpoint appears in it at all.

Two things worth knowing before wiring it:

- **A webhook alert cannot be threaded.** Slack incoming webhooks return no
  message `ts`, so there is nothing to reply to. That is a property of webhooks,
  not a gap here. Without a bot token the thread step reports that and skips —
  the BLUF line and the GitHub comment still carry the diagnosis.
- **Re-runs update, they do not stack.** Every comment carries a hidden marker
  and a repeat run PATCHes the existing one. A flaky job re-run five times would
  otherwise bury the PR, and that noise is what gets the mechanism switched off.

### "The log could not be read" is its own answer

A failed log *fetch* looks exactly like a short log with no known signature —
so without a guard it renders as "unrecognised failure", confidently, forever.
That is what happened the first time this ran for real (PR #3): the `notify`
job's `permissions:` block **replaces** the default scope set rather than adding
to it, so listing three write scopes silently removed `actions: read`,
`gh run view` returned 403, and the comment reported an unrecognised failure.

The output looked entirely plausible. **"No result" and "no result available"
must never render the same** — hence the `no-log` tier, which names the remedy
instead. The failure that taught us is now a row in the shared table, which is the
promotion loop working on the tool that implements it.

A second, subtler one followed: a job **cannot read its own run's archived
log** — the run is still in progress while the job executes. `gh run view
--log-failed` returned something non-empty but useless, and the tool faithfully
reported an unrecognised failure. Per-JOB logs are the fix, and are available as
soon as each job finishes.

## Tier 2 — model (not wired from GitHub)

An unmatched failure produces an escalation payload — redacted log excerpt,
`action: "advise"`, and a prompt that demands the model say so **first and
loudly** if its fix weakens a check. That payload is built and tested, but
**nothing sends it from a GitHub runner**: the router listens on loopback only
and is unreachable from CI. Today the payload is printed. Wiring it needs the
router reachable from the runner, which is an exposure decision, not a code one.

### An unrecognised failure becomes a ticket

A *known* cause needs its fix, not a ticket. An **unrecognised** one is state
that outlives the run — nobody has an answer yet — so the notify job files it
via `scripts/ticket.py` (GitHub Issues by default, swappable via
`TICKET_BACKEND`; see `ai-standards/references/ticketing.md`).

Keyed on the **condition**, `ci/<workflow>/unrecognised-failure`, never the run.
A re-run edits the one ticket instead of filing a fifth — which is what keeps a
tracker worth reading, the same way a six-line cap keeps an alert channel worth
reading.

Wired in this repo first rather than swept to all seven: a mechanism that files
things into a human's tracker should prove its noise level somewhere before it
does it everywhere.

## Tier 3 — human

The model proposes; a person decides. Every fix that lands is a merge someone
approved.

## Diagnosis is a root-cause review, not a symptom report

Asking "what's the cause" reliably returns the **proximate** cause and stops
there — the thing that broke, not the thing that let it break. Both the
[RCPS](RCPS-2026-07-26.md) and the failure that prompted it landed the same way:
the fix was obvious in a line, and the reason it survived seven commits was not.

So the escalation prompt asks for five things, and the known-cause rows carry
two fields instead of one:

| | Question |
|---|---|
| **Proximate cause** | what broke — quote the line, or say no line supports it |
| **Root cause** | why that was possible; ask why past the first answer, but stop when you leave the evidence |
| **Why not caught sooner** | a failure nobody was told about is a *second, separate* defect |
| **Fix** | smallest change that addresses this run |
| **Prevention** | the **mechanism** that stops recurrence |

`prevent` is a required field on every row, asserted by the corpus. "Be more
careful" is not a mechanism; a row with no known mechanism must say so rather
than dress an intention up as one. `fix` gets done because it turns the build
green — `prevent` is the half that gets skipped once it is.

### "Has this workflow ever passed?"

The single most load-bearing fact about a red build, and a run list does not show
it. A first-run failure and a regression look identical there and need opposite
responses: one means the gate was never satisfiable, the other means something
that worked stopped working. One workflow in this fleet failed **7 for 7** while
the search was for what the last commit had broken.

The `notify` job asks GitHub and passes the answer in (`--ever-green
yes|no|unknown`); the tool has no network by design. When the answer is `no`, the
comment leads with a banner telling the reader to suspect the **gate**, not the
commit — and it does so *even when a known cause matched*, because a matched
signature explains the symptom, not why nothing has ever satisfied the gate.
Unknown history stays silent rather than guessing.

## Promotion — the part that compounds

**A confirmed fix becomes a row in the table.** That is the whole point: the
second occurrence of any failure is free. Adding a row is data, not code —

The tables are split because `ci_diagnose.py` is **vendored byte-identical**
across every repo. One table would either drift — defeating the vendoring — or
leave nowhere to record a cause specific to one project, and the promotion loop
is the whole point.

| File | Scope | Vendored? |
|---|---|---|
| [`scripts/ci-known-causes.json`](../../scripts/ci-known-causes.json) | causes any repo could hit | yes, byte-identical; canonical copy in `ai-standards` |
| [`.ci-known-causes.json`](../../.ci-known-causes.json) | this project's own | no, hand-edited here |

They are unioned, and a local row **wins** on a duplicate `id`: a repo that has
re-diagnosed a shared cause in its own terms knows something the shared table
does not. A missing local table is normal; a missing *shared* table is a broken
install and reports `unavailable`, not "no known causes".

```jsonc
{
  "id": "short-stable-key",
  "match": ["substring that must appear"],   // ALL must match, case-insensitive
  "not_match": ["substring that rules it out"],
  "same_line": true,                          // all `match` terms on ONE line
  "cause": "what is actually wrong, one sentence",
  "fix":     "what a human should DO about this run",
  "prevent": "the MECHANISM that stops recurrence — required",
  "confidence": "high",                       // high = seen and fixed
  "first_seen": "YYYY-MM-DD"
}
```

Two rules learned by getting them wrong:

- **Never add a speculative signature.** Every row in the table was diagnosed
  and fixed for real. A guessed signature is a confident wrong answer waiting to
  fire, and a wrong cause costs more than no cause.
- **Use `same_line` when a rule name appears in both outcomes.** The security
  test prints `n8n: ROUTER_TOKEN uses required ${VAR:?} form` whether it passes
  or fails, so a whole-log signature matched a *successful* run. That false
  positive is pinned verbatim in
  [`tests/ci_diagnose_test.py`](../../tests/ci_diagnose_test.py).

## Why it cannot fix anything

The obvious version of this tool commits a fix and pushes. Look at what CI has
actually failed on here: a gate that was too strict, a check whose regex was
wrong, a token form. The cheapest way to make CI green is almost always to
**weaken a check** — so an agent rewarded for green output learns to file down
the safety net, at 3am, with nobody reading.

So it has no write path *at all*, not a disabled one. The test asserts this
structurally (AST, not substring): no `subprocess`, `shutil`, `requests` or
`urllib` import, and every `open()` read-only.

## Running it by hand

```bash
gh run view <run-id> --log-failed > ci.log   # fine by hand; the RUN is finished
python scripts/ci_diagnose.py --log ci.log
python scripts/ci_diagnose.py --log ci.log --bluf   # the one-line alert form
python scripts/ci_diagnose.py --log ci.log --json   # includes the escalation payload
```

To see exactly what would be posted, without posting it:

```bash
python scripts/ci_diagnose.py --log ci.log --json > diag.json
python scripts/ci_comment.py --diagnosis diag.json --target both --dry-run
```

It always exits 0. It is a report *on* a failure that already happened; exiting
non-zero would fail the notify job and hide the diagnosis behind a second red X.
