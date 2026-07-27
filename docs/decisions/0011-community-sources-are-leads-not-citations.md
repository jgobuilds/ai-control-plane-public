# Community sources produce leads; only primary sources may be cited

**Date:** 2026-07-26 · **Lens:** `ai-standards/references/dependency-security.md`,
`ai-standards/references/build-vs-adopt.md`, `ai-standards/references/cost-awareness.md`,
`ai-standards/references/pii-controls.md`

## Considered

The requirement: extend research from "watch our dependencies for changes that
move a decision we recorded" to "scout any topic of interest and improve our own
methods" — the motivating example being monitoring a practitioner Discord for
data-modeling and dbt practices worth emulating.

Facts come from the artifacts themselves, not memory. The enablement kit grades
its own sources in the kit's own sources ledger: *"Rank: 1
primary/authoritative · 2 practitioner-credible · 3 secondary/synthesized · 4
weak."* Its `NOTICE.md` states the licensing posture that makes the kit
defensible: *"Copyright protects expression, not ideas, facts, methods, or data;
this kit re-expresses ideas and facts from the sources below in its own words."*
That posture holds for published, attributable work. It does not hold for chat.

| Option | Cost | Verdict |
|---|---|---|
| **Lead-only** — community signal produces a *lead*; the lane must resolve it to a primary source before anything may be cited or proposed | **$0 in money.** Non-dollar: one resolution step per lead, and a real lag behind the leading edge | **Adopt** — keeps the kit's verifiability, which is the thing being sold |
| **Direct ingest** — treat community posts as sources, cite them in the kit | $0 in money; **unbounded** legal and reputational exposure — third-party copyrighted expression, Discord ToS, community norms, and a rank-4 citation in a kit sold on rank-1 sourcing | **Reject** — the exposure is not priceable, and it destroys the asset it feeds |
| **Primary sources only** — no community monitoring at all | $0; misses emerging practice by roughly a publication cycle | **Reject as the whole answer, keep as the floor** — it is what we do today, and it is why the CAP-02 gap has stayed open |
| **Licensed/commercial practitioner feed** (analyst subscriptions, paid communities) | **unpriced** — needs a costed spike against named vendors; likely metered per seat | **Defer** — revisit if lead volume from free sources proves too thin to work the gap list |
| **Human-only scouting**, no lane | $0 money; **all** toil, and it does not happen | **Reject** — the gap list has been static, which is the evidence |

## Chose

1. **A community source may produce a lead. It may never be a citation.** A lead
   is a pointer — a technique name, a tool, a claim worth checking. It carries no
   authority and never appears in `sources.md`, `NOTICE.md`, or any generated
   asset.
2. **A lead must resolve to a rank 1–2 primary source before it can propose
   anything.** Unresolved leads expire. Rank 3–4 resolutions are recorded as
   "checked, not adopted" so the same lead is not re-scouted forever.
3. **No verbatim community text crosses the boundary.** Not into the kit, not
   into an artifact, not into a prompt that produces one. Leads are extracted as
   *topics*, and the extraction runs through the scrubber because chat carries
   P1/P2 personal data by default.
4. **Scouting proposes; it never edits.** A proposed change to the kit or to a
   house lens goes through a human gate, the same shape `promote-skill` already
   uses to move content up the scope tree.
5. **Community ingest is ADR-gated per source.** Each one needs its ToS and
   community norms checked before it is added — that check is a human's call, not
   the lane's.

## Because

The kit is sold on a single promise — *"Check my work before you hire me"* — and
that promise is mechanical: every claim traces to a ranked, dated source. A
citation to a Discord message is rank 4 by the kit's own scale, so ingesting
community content directly would degrade the exact property the product is bought
for. The cheapest way to destroy a verifiability asset is to fill it with
unverifiable content.

The licensing analysis points the same way. `NOTICE.md` works because ideas and
facts are not copyrightable and the expression is our own. That reasoning applies
to a published handbook whose author chose to publish. It does not transfer to
identifiable individuals talking in a semi-private community, who did not.
Running a bot to harvest that conversation into something we sell is also a
breach of trust in a small field where the practice is bought on reputation —
a non-dollar cost larger than any licence fee.

**What this gives up: speed.** Community discussion runs ahead of published
sources, plausibly by a publication cycle. Lead-only means we will always be
slightly behind the leading edge of practice, and occasionally a good technique
will be unusable because nobody has written it down properly. That is accepted:
the kit competes on being checkable, not on being first. A practice we can cite
to dbt's own documentation is more persuasive to a buyer than one we cannot
attribute at all.

The rule also happens to make the lane cheaper. Resolving to a primary source is
a deterministic retrieval step, not a judgement call, so it runs before any model
call — the same deterministic-before-models ordering the router already enforces.

## Status

**Decided, not built.** The design is written up in
[`docs/plans/RESEARCH-AND-MARKETING-LANES.md`](../plans/RESEARCH-AND-MARKETING-LANES.md)
§3. Nothing ingests community content today.

Verified by reading the artifacts, not from memory: the rank scale in
the kit's own sources ledger, the licensing posture in the kit's NOTICE,
and the open CAP-02 gap in the kit's coverage report (missing guide, poc, marketing)
that motivates the lane. **Assumed, not verified:** that free primary sources in
the analytics-engineering domain are rich enough to work the gap list without a
paid feed. The first ten leads will show whether that holds.

**Revisit when:** (a) a target community publishes under a reuse-permitting
licence such as CC-BY, which would move it from lead to citable source; (b) ten
leads resolve at under 30% to rank 1–2, meaning free primary sources are too thin
and the paid-feed option needs its costed spike; or (c) we want to cite
practitioner *consensus* as evidence in its own right — that requires a survey
methodology with consent, not scraping, and is a different decision.
