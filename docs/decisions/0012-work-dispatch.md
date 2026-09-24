# One dispatcher assigns work; agents never self-claim, so the atomic lease is not needed yet

**Date:** 2026-07-26 · **Lens:** `ai-standards/references/concurrency-and-branching.md`,
`build-vs-adopt.md`, `cost-awareness.md`, `notification-taxonomy.md`,
`ai-control-plane/docs/design/AGENT-DESIGN.md`

Follows [ADR 0010](0010-agent-work-management.md), which decided to build a task
record (N1) and an **atomic execution lease** (N2) into the router. This ADR
answers the question 0010 left open — *how work reaches an agent* — and in doing
so removes N2 from the critical path.

## The question

Several agents, a growing set of tickets. How does an agent learn there is work,
take it without another agent taking the same thing, and hand it on?

Three shapes were on the table: a chat channel, a message bus, or the ticket
store itself.

## Considered

Facts came from probing this stack — `docker-compose.yml` (no datastore),
`lanes/server.py` (how n8n drives Python today), `router/server.js` (the
chokepoint), `docs/design/AGENT-DESIGN.md` §5 (the handoff contract already
specified) — and from ADR 0010's own option table, not from re-researching the
market.

| Option | Cost | Verdict |
|---|---|---|
| **A. Central dispatcher assigns; agents never self-claim** — one scheduled lane polls the ticket store, picks one claimable ticket, assigns it, and POSTs the handoff to `/route` | **$0, no new service.** Non-dollar: a lane and an endpoint, ~a day. The dispatcher is a single point of failure for *throughput* (not for correctness — an unassigned ticket simply waits) | **Adopt** — single-writer removes the claim race by construction, so no compare-and-set is needed to be correct |
| **B. Agents self-claim from the ticket store** (poll, then claim) | $0 in money. Non-dollar: **needs a real atomic lease** — compare-and-set plus TTL plus crash recovery, which is ADR 0010's N2 and is blocked on the unverified G10a spike | **Defer** — it is where this goes when one dispatcher is the bottleneck, and the trigger is named below |
| **C. Slack as the coordination channel** — agents post and read work in a channel | $0 (already there). Non-dollar: **high and permanent** — chat is unordered, lossy, has no state, no idempotency and no delivery guarantee; every one of those has to be rebuilt on top | **Reject** — `notification-taxonomy` already scopes Slack as the human *interrupt* tier. Work carried in an alert channel is a queue with none of a queue's properties |
| **D. A real message bus** (Redis Streams, NATS, RabbitMQ) | $0 licence; **fixed infra** — a stateful service to run, back up and patch, which ADR 0009 established this platform cannot reliably host | **Reject for now** — and note that broadcast is the *wrong shape* anyway: publishing "ticket 12 needs work" to N agents gets it done N times unless exclusive delivery is added, at which point the bus is a queue and the tracker already is one |
| **E. Adopt a durable-execution engine for dispatch** (Temporal, DBOS, Hatchet) | $0 licence; fixed infra + real operational skill | **Reject for dispatch** — 0010 already scoped these to N3 (resumable execution). Dispatch is not the hard part and does not justify the tier |
| **F. A2A as the transport** | $0 licence; unpriced integration effort | **Defer — watch, do not adopt.** v1.0 under the Linux Foundation AAIF alongside MCP, but it is a protocol for agents across *organizational* boundaries. Intra-fleet dispatch through our own router does not need it, and adopting it now adds a second object model beside our own task record |

## Chose

1. **One dispatcher assigns. Agents never self-claim.** A scheduled lane polls
   for claimable tickets, selects one, marks it assigned, and POSTs a handoff to
   the router. The router remains the only thing that reaches a runner.
2. **Therefore N2 (the atomic lease) is deferred, not built.** With a single
   writer there is no claim race to lose. **G10a — can n8n Data Tables do an
   atomic conditional update — stops blocking**, because nothing depends on the
   answer until agents self-claim.
3. **The ticket is the queue.** No new bus, no new datastore. Pull, never push.
4. **The handoff payload is the one already specified** in `AGENT-DESIGN.md` §5
   — `objective`, `acceptance`, `context`, `owner`, `hops` — carried as a
   structured comment on the ticket, with `hops` enforced as the loop guard.
5. **One agent per ticket.** Two agents on one ticket is contention; the rule
   is already `one agent = one worktree = one branch`. Work that needs two
   agents is two tickets.
6. **Slack stays the human interrupt tier**, carrying a BLUF line and a link.
   It is never the transport.
7. **GitHub Issues remains dogfooding-only.** ADR 0010 option H rejects
   bring-your-own trackers for *tenant* work — tenant-linked metadata to a third
   party contradicts the no-external-processor positioning. `scripts/ticket.py`
   is backend-swappable precisely so that boundary is enforceable rather than
   remembered: tenant work uses `file` or the router, never `github`.

## Because

**The chokepoint doctrine already in force answers the hard part for free.** The
standing invariant is *"n8n calls the router, not the runners"* — one enforcement
point. Applying the same shape to assignment makes the dispatcher the single
writer of ticket ownership, and a single writer cannot race itself. That is not a
workaround for the missing lease; it is the same architectural argument that
produced the router, applied one level out.

**This is deliberately the smaller thing.** ADR 0010 observed that we *have the
gap, not yet the pain* — no scheduled lane had ever fired autonomously at the
time it was written. Building compare-and-set, TTL and crash recovery before a
second agent has ever contended is the dependency-bloat failure `build-vs-adopt`
names, paid for in a stateful tier ADR 0009 says this platform cannot host.

**What this gives up, stated plainly.** Throughput is bounded by one dispatcher,
and a dispatcher that dies stops the flow of new work until it is restarted. That
is a real limitation and it is the *correct* failure mode: work waits rather than
being done twice. It is tolerable while the fleet is small and stops being
tolerable at the trigger below — at which point option B is the successor, and
the lease has to be built properly rather than approximated.

**And the failure this avoids is worse than the one it accepts.** Duplicate
execution is not a throughput problem, it is a correctness problem: two agents on
one ticket produce conflicting commits, double-post, or double-spend a rate
limit. Bounded throughput is recoverable by restarting a lane. Duplicated
side effects are not.

## Agent identity — the claim is a label, not an assignee

Decided by probing GitHub, and the probe changed the answer twice.

`repos/:o/:r/assignees` lists only real collaborators, so **a persona cannot be
a GitHub assignee**. And `NAMING.md` rule 6 already said personas are
*"a human-facing label … never used as a technical key"* — so making the claim
an `agent:<persona-slug>` label is that rule taken literally, not a workaround.

The second finding is the load-bearing one: **`gh issue edit --add-assignee`
prints "Could not resolve to a user" and exits 0.** A return-code check reports
a successful claim while claiming nobody — and here the claim *is* the mutual
exclusion, so that silently re-serves one ticket to every agent forever. It
would have looked like success from every angle.

So `assign()` writes the label and **reads it back**, refusing to report success
otherwise. The GitHub assignee keeps its ordinary meaning: the accountable
human. The dispatcher's persona is **Dex the Dispatcher**.

## Status

**Decided, built and ACTIVE** — `aicp-dispatch`, every 10 minutes. Verified by
probe: no datastore in the stack (`docker-compose.yml` volumes), the router is
the only path to a runner, `lanes/server.py` is the established pattern for
n8n-driven Python, and the handoff payload contract already exists in
`AGENT-DESIGN.md` §5 rather than being invented here.

**Verified by probe after activation:** `active = 1` in n8n's own database (the
first read missed it — the WAL has to be copied alongside the `.sqlite` file or
recent writes are invisible), and the dispatcher runs inside the `lanes`
container against a live backend.

**The backend is `file`, not `github`, and that is a real limitation.** The
`lanes` container has no `gh` binary and no GitHub credential. Reaching GitHub
Issues from there needs either `gh` in the image or a token plus an API
transport — and putting a credential in that service is an **exposure decision**,
not a code change. `TICKET_BACKEND=github` is a one-word switch once it is made,
which is what the swappable seam bought.

**Assumed, not verified:** that one dispatcher is fast enough. Nothing measures
dispatch latency yet.

**Revisit when** any of these is true:

- **a second dispatcher is wanted** for throughput, or dispatch latency becomes
  the complaint — then build N2 properly and move to option B
- **an agent needs to claim work the dispatcher cannot see** (an agent
  discovering its own follow-up work mid-task)
- **the dispatcher's restart-to-recover becomes unacceptable** — that is the
  durable-execution trigger from 0010 (N3, presumptive choice DBOS), not this one
