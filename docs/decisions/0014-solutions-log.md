# Reject the compound-engineering plugin; borrow its `solutions/` format, because our promotion loop only covers CI

**Date:** 2026-07-27 · **Lens:** `ai-standards/references/build-vs-adopt.md`,
`knowledge-for-agents.md`, `agent-context-architecture.md`,
`change-safety.md`

Extends the promotion loop from [ADR 0012's](0012-work-dispatch.md) neighbourhood
— the known-cause table in `scripts/ci-known-causes.json` — to the problems that
table structurally cannot hold.

## The question

`EveryInc/compound-engineering-plugin` (23,519★, MIT) ships 32 skills across
eight agent hosts. Its centrepiece, `/ce-compound`, documents a solved problem
into `solutions/` with YAML frontmatter, and its stated rationale is *"the first
time you solve a problem takes research; document it, and the next time is
free."*

That is our promotion loop, in the same words. So the question is not whether the
idea is good — we already believe it — but whether to adopt their implementation,
and whether ours has a gap theirs would fill.

**It does have a gap.** `ci-known-causes.json` matches **log signatures**. The
most valuable lessons of 2026-07-26/27 were not log signatures: an n8n schedule
that does not register after a CLI activation, a `lanes` container serving code
seven hours old because `server.py` is baked into the image while `./scripts` is
bind-mounted, an IF node that could not survive the null its own payload uses for
"nothing to do". Those were filed into `.ci-known-causes.json` **because there
was nowhere else to put them**, and none of them is a CI cause.

## Considered

Facts from the GitHub API and from reading `skills/ce-compound/SKILL.md` and
`CONCEPTS.md` directly (2026-07-27): MIT, 23,519★, created 2025-10-09, pushed
2026-07-24, **86 open issues**, 32 skills, no credential requirement. Compared
against this repo's actual assets: 16 known-cause rows, 6 runbooks, one RCPS.

| Option | Cost | Verdict |
|---|---|---|
| **A. Adopt the plugin wholesale (32 skills)** | $0 licence (MIT). Non-dollar: **high and permanent** — 32 instruction surfaces against a 12,000-char always-loaded budget that exists because bloat measurably reduces adherence; most duplicate `engineering-standard`, the concurrency lens, or Claude Code built-ins, and overlap is where conflicting guidance lives | **Reject** — we would pay context for capability we have, and inherit 86 open issues on an auto-updating marketplace install |
| **B. Cherry-pick 3–4 of its skills** | $0 licence; moderate non-dollar — a partial vendoring with no drift check, of prompts written against another project's conventions | **Reject** — the value is in one *format*, not in the prompts. Vendoring prose we would immediately rewrite is the worst of both |
| **C. Reject the plugin; borrow the `solutions/` format, cited** | $0. Non-dollar: ~half a day for a script, a reference and a gate; then one file per solved problem, written while the context is fresh | **Adopt** — it closes a real gap, costs no context budget until an agent reads a specific solution, and keeps the enforcement model we already have |
| **D. Do nothing — keep only `ci-known-causes.json`** | $0 | **Reject** — it is what we are already doing, and it is why non-CI lessons are being filed into a CI table. The mismatch will grow, not resolve |
| **E. Widen `ci-known-causes.json` to hold non-signature problems** | $0 | **Reject** — it would destroy the property that makes it useful. That table is *matched*, deterministically, and a row with no signature cannot be matched. Mixing them leaves a table that is neither |
| **F. Adopt their multi-host skill converter** (separate from the above) | $0 licence; unpriced integration | **Defer — real prior art.** Ours converts instruction files across five hosts; theirs converts whole skills across eight. Worth reading before re-deriving, if we ever want `ai-standards` skills usable from Cursor or Codex |

## Chose

1. **Do not install the plugin.** Cite it as the source of the format.
2. **Add a `solutions/` log**, standardised in `ai-standards`
   (`references/solutions-log.md` + `scripts/solution.py`) and populated per repo,
   so the format is shared and the content is local — the same split as the
   known-cause tables.
3. **Keep the two records distinct, by a rule that decides every case:**

   > **Does the problem announce itself in a log line?** If yes it is a
   > known-cause **row** — matchable, deterministic, gated. If no it is a
   > **solution** — read by a human or an agent that went looking. Some problems
   > earn both: the row matches it, the solution explains it.

4. **Frontmatter carries `id`, `last_verified` and `status`**, per
   `knowledge-for-agents.md`, so solutions join the same freshness and catalog
   discipline as every other knowledge atom rather than becoming a folder of
   undated prose.
5. **Gate the shape, not the prose.** CI validates frontmatter and unique ids. It
   does not grade the writing, because a gate that argues about quality gets
   switched off.

## Because

**The gap is real and specific.** Six known-cause rows in the local table are not
CI causes. They are operational lessons wearing a CI table's schema because that
was the only structured place to put them — and a record whose shape is wrong is
one nobody trusts to be complete.

**But adopting 23.5k stars of prompts to fix a filing problem is the wrong
trade.** `agent-context-architecture` is explicit that longer instruction
surfaces reduce adherence, and this repo enforces that with a budget gate. Thirty-
two skills is not free; it is the most expensive kind of dependency, because it
competes for the context the work needs. The format costs nothing until read.

**And their model is advisory where ours is enforced.** `/ce-compound` asks an
agent to document a solution. Our comparable mechanisms *fail the build*:
`adr_check.py` on a missing Cost column, `check_links.py` on a dangling link,
`staged_scope_check.py` on a foreign staged file. Today's own RCPS concluded that
**a rule in a document is not a mechanism** — so borrowing the format while
keeping our enforcement model is the version of this idea that survives contact.

**What this gives up, stated plainly.** Their 32 skills contain work we are not
getting: a browser-test skill, an Xcode-test skill, PR babysitting, a strategy
skill. Some of those are genuinely useful and we are declining them to protect a
context budget. If any becomes a real need, adopt *that one*, namespaced, rather
than reopening the whole plugin.

## Status

**Decided; the format and its tooling are being built alongside this record.**
Verified by reading their `SKILL.md` and `CONCEPTS.md` rather than the README,
and by counting our own assets (16 rows, 6 runbooks, 1 RCPS) rather than
estimating them.

**Assumed, not verified:** that solutions get written while the context is fresh.
Nothing enforces that, and the failure mode is a log that is thorough for one
week and empty thereafter.

**Revisit when:**

- **The solutions log has fewer entries than the month had incidents** — that is
  the assumption above failing, and the fix is a prompt at the moment of
  resolution, not more discipline.
- **A solution would be better as a matchable signature** — move it; the rule
  above is the test.
- **We want `ai-standards` skills on Cursor or Codex** — read option F's
  converter first.
