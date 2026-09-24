# Adopt `last30days` for human-tier market research only — never on a lane, never with tenant terms

**Date:** 2026-07-27 · **Lens:** `ai-standards/references/build-vs-adopt.md`,
`dependency-security.md`, `evidence-standard.md`, `cost-awareness.md`,
`references/knowledge-for-agents.md`

Complements [ADR 0010](0010-agent-work-management.md) (no external processor for
tenant work) and the existing `research-watch` lane, which this does **not**
replace.

## The question

`scripts/research_watch.py` answers one narrow question well: *has anything moved
a decision we already recorded?* It is deliberately silent otherwise — "a release
that does not move a decision is noise, and this deliberately does not report
it."

It cannot answer the other question: *what do practitioners actually say about X
right now?* That gap is real — pre-sales research for the consultancy, the
landscape pass in `vet-idea`, and chasing a claim like R5 to a primary source all
need community signal we currently gather by hand.

## Considered

Facts from the GitHub API and from reading the skill's own `SKILL.md`
(2026-07-27), not from the README's marketing: MIT, 53,901★, created 2026-01-23,
last pushed 2026-07-26, **73 open issues**. Its setup requires `APIFY_API_TOKEN`,
`AUTH_TOKEN` and `TRUTHSOCIAL_TOKEN` for the non-free sources, and its own docs
state the install is *"a git clone Claude Code auto-restores to `origin/main` on
session start."*

| Option | Cost | Verdict |
|---|---|---|
| **A. Adopt `last30days`, human-tier only, with guardrails** | **$0 licence (MIT).** Quota-limited free for Reddit/HN/Polymarket/GitHub. **Metered** above that — Apify billing triggers on any run touching X or TikTok. Non-dollar: a credential surface (browser sessions + three tokens) on a **self-updating** clone, and a standing discipline not to type a tenant name into it | **Adopt** — it fills a real gap, and every risk below is containable by *where* it runs rather than by trusting it |
| **B. Adopt it fully — wire it to a scheduled lane** | Same licence; **metered and unbounded**, since a schedule multiplies Apify calls with no human in the loop | **Reject** — 73 open issues on a three-month-old project is fine for a tool you invoke and read, and not fine for one that runs unattended against a metered API |
| **C. Build our own multi-source searcher** | $0 licence; **high** non-dollar — a dozen platform APIs, each with its own auth, rate limits and breakage, forever | **Reject** — this is the NIH half of build-vs-adopt. The hard part is the platform bridging, which is exactly what the 53.9k★ buys |
| **D. Status quo — the host's built-in web search plus manual browsing** | $0 | **Reject as sufficient, keep as fallback.** Web search does not reach Reddit comments, X, or YouTube transcripts, which is precisely the signal being sought |
| **E. A paid research API (Perplexity, Exa, and similar)** | **Metered**, per query | **Defer** — cleaner operationally, but it is another external processor and would need clearing under ADR 0010's rule before any tenant-adjacent use |
| **F. Extend `research_watch.py` to cover community sources** | $0 licence; moderate non-dollar | **Reject** — it would destroy the property that makes that lane useful. Its value is that it stays *silent*; adding a firehose to a signal detector leaves you with a firehose |

## Chose

Adopt **A**, with three guardrails that are the decision, not caveats on it.

1. **Human tier only.** A person invokes it and reads the output. It does **not**
   go in a runner — those are default-deny by design and this needs broad egress
   — and it does **not** go on a lane.
2. **Never with tenant-identifying terms.** Querying it about a tenant sends that
   name to a dozen third-party APIs. ADR 0010 rejects external processors for
   tenant work because the no-external-processor stance *is* the niche; a
   convenient research tool is exactly how that stance erodes quietly.
3. **Community output is a LEAD, never a citation.** `evidence-standard.md`
   already says this. Anything it surfaces that would go into a tenant
   deliverable, the method, or an ADR must be traced to a primary source with a
   `Pulled:` date first.

Plus two operational constraints:

4. **Free sources by default.** Reddit, HN, Polymarket and GitHub need no token.
   Configuring Apify turns a free tool into a metered one — do that deliberately,
   per query, not as part of setup.
5. **The auto-update is the supply-chain risk to watch.** Not the star count.

## Because

**The gap is real and the build option is worse.** The hard part is bridging a
dozen walled gardens, each with its own auth and rate limits. That is
maintenance we would carry forever to duplicate something MIT-licensed and
actively developed. `build-vs-adopt` names both failure modes, and this is
squarely the NIH one.

**But popularity is not a security argument.** `dependency-security.md`'s
privileged-path principle says the bar scales with *position*, not popularity —
and this asks for browser sessions and three API tokens, then installs as a clone
that **restores itself to `origin/main` on session start**. That combination is a
privileged path with an unreviewed update channel. 53.9k stars does not change
it; it arguably makes it a more attractive target.

The guardrails are chosen so the risk is bounded by **placement rather than
trust**. At the human tier, with free sources, an unreviewed update can produce a
bad *answer* that a person reads and judges. On a lane, with tokens and a
schedule, the same update runs unattended against a metered API with our
credentials. Same software, different blast radius — and the containment
argument here is the same one that keeps the runners firewalled.

**What this gives up, stated plainly.** No scheduled community-signal digest,
which is the obvious next thing to want and the thing option B would buy. We are
trading that for a bounded credential surface, and the trade stops being right
the moment the project stabilises enough to trust unattended — which is the
trigger below, not a permanent position.

**And what it does not change.** `research_watch.py` stays exactly as it is. The
two answer different questions, and the reason to keep ours is the property a
discovery engine cannot have: it stays quiet unless a recorded decision moved.

## Status

**Decided, not yet installed.** Verified by API and by reading `SKILL.md`:
licence MIT, 73 open issues, created 2026-01-23, and the three token names plus
the self-restoring clone are quoted from the skill's own documentation rather
than inferred.

**Not verified:** what Apify actually costs per run at our query shape, and
whether the free sources alone are sufficient for pre-sales research. Both are
answerable only by using it.

**Revisit when** any of these is true:

- **A first real use shows the free sources are not enough** — then price Apify
  properly before enabling it, rather than discovering the bill.
- **The project stabilises** (issue count trending down, a tagged release
  cadence) — then reconsider option B for a scheduled digest, and pin a commit
  rather than tracking `main`.
- **Anything it surfaced reaches a tenant deliverable without a primary source
  behind it** — that is guardrail 3 failing, and it means the discipline needs a
  mechanism rather than a rule.
- **A second operator joins** — guardrail 2 is currently enforced by one person
  remembering it, which does not survive a team.
