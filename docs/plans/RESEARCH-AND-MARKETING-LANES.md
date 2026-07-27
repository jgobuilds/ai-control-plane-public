# Research and marketing lanes — design

**Status:** design only. No lane code is written. Approve or amend before build.
**Date:** 2026-07-26

The ask was "turn the enablement work into research and marketing agents inside
the control plane." Investigation changed the shape of that: **research is
already built and simply not switched on**, and marketing is a genuinely new
lane whose main design problem is keeping org-specific content out of a
brand-neutral engine.

---

## 1. Research — built, dormant, needs activation not design

`n8n-workflows/research-watch.workflow.json` exists and is complete:

```
Weekly (Mon 07:30) ─┐
Run on demand ──────┴─► Fetch research ─► Triage findings ─► Anything to
                        (lanes /research)   (code)            reason about?
                                                                │
                          ┌─────────────────────────────────────┴──── no ──► Format quiet week
                          └── yes ──► Ask the agent ─► Format proposal ──┬──► Notify
                                      (router /route)                    ┘
```

It is backed by `scripts/research_watch.py` and the `/research` endpoint in
`lanes/server.py`. `active` is unset, and the README's live-lane table lists only
`daily-digest`, `eval-drift` and `retention-sweep`.

**Two design properties are already right and must not be "improved" away:**

- **It is not a news feed.** Every watched source is tied to a decision already
  recorded in `docs/decisions/`. A release that does not move one of our own
  revisit-triggers is noise, and the lane deliberately does not report it.
- **Egress happens in `lanes`, never in a runner.** The runners are firewalled
  default-deny — verified 2026-07-25, `github.com` times out from
  `claude-runner`. The lane fetches, writes a file, and the firewalled runner
  reasons over the file. Widening a runner's allowlist to let it browse would
  hand back the containment property.

**DONE 2026-07-26 — activated.** `brightworks-research-watch` now appears in
`n8n list:workflow --active=true`, scheduled Mon 07:30.

Verified before activating, by exercising the endpoint inside the container with
the service token: 6 sources reached, 19 releases in a 7-day window, 2 notable,
1 version drift, `checked_all_sources: true`, `unreachable: 0`, `actionable:
true`. So the egress half is proven live, not assumed.

Two things could not be verified this way and remain assumptions:

- `scripts/n8n_bootstrap.py --dry-run` **cannot run while the stack is up** —
  `n8n execute` spawns a second instance and collides on task-broker port 5679.
  So the n8n-side path (triage → IF → router → notify) has not been executed
  end-to-end; only the lane endpoint it calls has.
- A **quiet-week heartbeat** has not been observed, because this week is
  actionable. Watch for it on a week where nothing moves.

The first real fire is the test. The README warns separately that a suspended
Docker VM silently swallows missed crons — if Monday passes with no message,
that is the first thing to check, not the lane.

---

## 2. Marketing — new, and the design constraint is brand neutrality

### The problem to solve first

This engine is AGPL-3.0 and intended to go public. `check_public_hygiene.py`
blocks brand tokens and `marketing/` paths from ever entering it. The enablement
kit — the actual marketing knowledge — is **proprietary content in a separate,
private repo**, not in this one.

So the lane cannot contain content, positioning, or voice. It must be a generic
mechanism — *draft from whatever knowledge base is mounted in the active scope* —
with the org-specific parts arriving exactly the way brand tokens already do:
through the instance overlay and the scope tree.

That is the whole design. Everything below follows from it.

### The knowledge source plugs in, it does not get imported

The kit is already machine-readable in a way that suits this perfectly:

| Artifact | What it gives the lane |
|---|---|
| `kit.json` | 270 atoms with stable IDs, titles, files, anchors — a resolvable index |
| `coverage.json` | **which capabilities lack a marketing asset** |
| `knowledge/*.md` | sourced claims the draft must cite rather than invent |
| `sources.md` | the evidence ledger behind every claim |

`coverage.json` is the find that makes this lane worth building. The kit's own
build already reports, today:

```
CAP-02 … missing guide, poc, marketing
CAP-03 … missing marketing
CAP-04 … missing marketing
CAP-05 … missing marketing
CAP-06 … missing poc, marketing
CAP-07 … missing poc, marketing
CAP-10 … missing guide, poc, marketing
```

**That is a prioritized content backlog, generated deterministically, with no
model involved.** The lane does not need to invent what to write about; it needs
to work the gap list. This is the deterministic-before-models rule applied to
content.

The kit reaches the scope by the route the framework already has for exactly
this — `drive-sync`, which pulls content through the scrubber into scope — or by
mounting the kit read-only into the scope's workspace directory. Either way the
engine ships with no org-specific content in it.

### Lane shape

Composed from sub-workflows that already exist. Nothing new is invented:

```
Weekly (Thu 07:00) ─┐
Run on demand ──────┴─► Read backlog ──► Worth drafting? ──no──► Heartbeat ──► Notify
                        (lanes             (code, IF)
                         /content-gaps)         │ yes
                                                ▼
                                          Ask the agent  (router /route,
                                           task_type=draft, action=advise)
                                                │
                                                ▼
                                          Draft → scope outbox
                                                │
                                                ▼
                                          approval-gate  ◄── HUMAN, always
                                                │
                                     ┌──────────┴──────────┐
                                  approved              rejected
                                     │                      │
                                     ▼                      ▼
                              deliverables lane        Notify (with reason)
                              (already ships to
                               Drive, gated)
```

**Endpoints and contracts, all existing:**

- `http://lanes:8081/content-gaps` — new endpoint, same pattern as `/research`
- `http://router:8080/route` with `{prompt, task_type, action, trigger}`
- `approval-gate` sub-workflow — build request → notify approvers → wait → decision
- `brightworks-notify` sub-workflow — channel-abstracted, Slack/Teams/webhook by env

### Five rules this lane must obey

1. **Never auto-publish.** Publishing is irreversible and outward-facing. The
   approval gate is not configurable-to-off for this lane, unlike
   `approval-gate`'s auto-approve path used elsewhere. A draft that ships
   without a human read is the failure mode that matters here.
2. **Cite, don't invent.** Every claim resolves to an atom ID from `kit.json`.
   A draft containing an unresolvable `[CAP-nn]` fails the lane, the same way
   `build_site.py` already fails on a broken reference. This is the existing
   honesty mechanism, reused.
3. **Gate the model call.** If the gap list is empty, skip the agent entirely and
   post a heartbeat. Most weeks should cost nothing — this is what
   `research-watch`'s triage step already does, and why.
4. **Reuse `deliverables` to ship.** It already handles rehydration, approval,
   Drive upload, and archiving to `sent/`. Building a second shipping path would
   be exactly the duplication `build-vs-adopt` forbids.
5. **Render in the loaded token set.** Any human-facing artifact uses `--brand-*`
   from the active overlay, never a hardcoded hex — the `brand` lens.

### What this lane is not

Not a social scheduler, not an analytics reader, not a campaign manager. Those
are the `small-business` plugin's job and sit outside this engine. This lane
closes gaps in a sourced knowledge base and routes the results through a human.

---

## 3. Practice-scout — learn from the field, improve our own methods

A third lane, and a different animal from the first two. Where `research-watch`
monitors things we depend on, this scouts practice we do not yet have. Governed
by [ADR 0011](../decisions/0011-community-sources-are-leads-not-citations.md).

### Different question, same discipline

| Lane | Asks |
|---|---|
| `research-watch` | Did this move a **decision we recorded**? |
| `practice-scout` | Does this name a practice we **lack**, in a capability we **flagged as a gap**? |

Both key off a deterministic list rather than a feed — one off ADR
revisit-triggers, the other off `coverage.json`. That is what stops it becoming
a firehose, and it is the property to protect if the lane is ever extended.

The gap list is not hypothetical. `coverage.json` reports **CAP-02 — "Data
modeling & transformation (analytics engineering)" — missing guide, poc *and*
marketing**: the largest hole in the kit, and exactly the dbt/modeling territory
worth scouting. The lane does not choose topics; the build already did.

### The lead → source pipeline

```
community signal (Discord / Slack / HN / forums)
        │  extract TOPIC only — through the scrubber, no verbatim text
        ▼
      LEAD   "people keep citing X for incremental models"
        │  deterministic retrieval, before any model call
        ▼
  PRIMARY SOURCE   dbt docs · published handbook · conference talk
        │  rank 1–4 per method/sources.md
        ▼
   rank 1–2 only ──► proposal ──► HUMAN GATE ──► kit or lens update
   rank 3–4 ──────► recorded as "checked, not adopted" (stops re-scouting)
```

**The community tells us what to go read. It is never the citation.** Full
reasoning, options and costs are in ADR 0011; the short version is that the kit
is sold on every claim tracing to a ranked dated source, so filling it with
unverifiable content destroys the property it is bought for.

### Two targets, two bars

- **The kit** (external method) — anchored on `coverage.json`. A proposal must
  clear the kit's existing quality bar: fillable template, acceptance test,
  failure modes.
- **House processes** (`ai-standards` lenses, ADRs) — a higher bar, set by how
  those lenses were written: `change-safety` says *"each rule written after a
  real failure"*; `dependency-security` came from a verified CVE survey. A
  scouted practice arrives as a **proposal with evidence**, never an edit.
  `promote-skill` already models the gated shape.

Note this is *not* the same loop as [`IMPROVEMENT-LOOP.md`](../design/IMPROVEMENT-LOOP.md),
which closes agent **output quality** via offline regression eval. This one
improves **method**. They share a philosophy — local, governed, human-gated — and
nothing else.

### Sourcing order

1. **Zero-auth primary sources first** — dbt docs and Developer Blog, Locally
   Optimistic, the Analytics Engineering Roundup, GitLab's handbook (already
   `SRC-01`). No credentials, no PII, cleanly citable. For CAP-02 specifically
   these may be sufficient on their own.
2. **Community lead sources second**, one ADR per source, lead-only, scrubbed,
   never quoted. Each needs its ToS and community norms cleared by a human
   before it is added — that is a judgement call, not a lane's.

---

## 4. Sequence, and what to decide

| Step | Effort | Blocked on |
|---|---|---|
| Activate `research-watch`, verify a live fire + quiet-week heartbeat | small | nothing |
| Decide how the kit reaches scope — `drive-sync` vs read-only mount | — | **your call** |
| Build `lanes /content-gaps` over `coverage.json` + `kit.json` | small | the above |
| Build the `content-draft` workflow from the shape above | medium | the above |
| ADR for the marketing lane, incl. the Cost column | small | build |
| Build `practice-scout` over zero-auth primary sources | medium | the kit reaching scope |
| Clear ToS + community norms for any chat source | — | **your call, not the lane's** |
| Per-source ADR before any community source is added | small | the above |

**DECIDED 2026-07-26 — the kit reaches scope by `drive-sync`.** Not a read-only
mount of the kit repo's `method/` tree.

Because a mount would give this engine a filesystem path into a proprietary
sibling repo, which is exactly what the brand overlay avoids by resolving
`*-instance` generically. `drive-sync` already exists, already routes through the
scrubber, and needs no new mount surface — so the engine stays publishable with
no knowledge that the kit repo exists.

What that costs, stated plainly: a copy of the kit lives in Drive, so it can go
stale against `method/`. The mitigation is the one already in the repo — the kit
is rendered from source on every deploy, and `drive-sync` watermarks each sync,
so staleness is visible rather than silent. It also means Drive OAuth becomes a
prerequisite for the content lane; `drive-sync` and `deliverables` are both
marked "needs OAuth" today.
