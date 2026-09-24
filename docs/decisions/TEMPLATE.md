# <Decision in one line — what was decided, not what was discussed>

**Date:** YYYY-MM-DD · **Lens:** `ai-standards/references/<lens>.md` (list the lenses
that actually shaped the call)

## Considered

State where the facts came from (API, primary doc, a probe of the running system) —
not a rendered page or memory. Then compare the options **including cost**, because a
comparison without cost gets decided on architecture and paid for later.

| Option | Cost | Verdict |
|---|---|---|
| <option> | <class + trigger, e.g. "quota-limited free; meters above 1 GB/day"> | **Adopt** / Reject / Defer — one clause of why |
| <option> | <e.g. "$0 in money; ~weekly re-consent toil"> | Reject — … |
| <option> | unpriced — needs a costed spike | Defer — … |

Cost guidance (`cost-awareness.md`): name the **class** (free / quota-limited free /
metered / fixed), state the **trigger** rather than a figure that goes stale, label
**notional vs real money**, and count **non-dollar costs** — toil, verification,
egress, storage growth, the cost of leaving. "Needs a billing account" is a decision
input, not a footnote. Never leave a Cost cell blank; write "unpriced" and say what
would price it.

## Chose

1. …
2. …

## Because

The reasoning that would let a future reader disagree with you on the merits — the
trade-off accepted, not just the benefit claimed. Name what this decision gives up.

## Status

Implemented / partially implemented / decided-not-built. Say what is verified and
**how**, and what is still assumed.

**Revisit when:** a concrete trigger — a volume, a count, a date, a second machine.
A deferral without a trigger is an omission wearing a decision's clothes.
