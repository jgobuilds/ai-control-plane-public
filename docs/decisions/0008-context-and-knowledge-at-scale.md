# Context and knowledge across agents at scale: defer the graph, keep the trigger

**Date:** 2026-07-25 · **Lens:** `ai-standards/references/build-vs-adopt.md`,
`ai-standards/references/cost-awareness.md`,
`ai-standards/references/agent-context-architecture.md`

Revisits the memory/knowledge-layer deferrals in
[`0001-context-compression.md`](0001-context-compression.md) with 2026 evidence.

## Considered

**The problem, stated well by [Atlan](https://atlan.com/context-and-chaos/issue/i-got-long/):**
as agents multiply, each re-authors the same business definitions — an *authoring
tax* — and the failures are **silent**: *"When two agents disagree, nobody sees
it."* The investment "does not compound. It restarts." Their prescription is
governance + an open format + **runtime delivery**.

We already have the first two (`context/scopes.json`, `context/model-policy.json`,
the `workspace/**/CLAUDE.md` hierarchy — governed, versioned, open formats). What
we have for runtime delivery is a **file hierarchy**, which is exactly the thing
that degrades at scale.

| Option | Cost | Verdict |
|---|---|---|
| **Status quo** — per-scope CLAUDE.md hierarchy + `scopes.json`, delivered as files | **$0**; authoring tax grows with agent count | **Keep for now** — we are on the near side of the window (~5 agents, one operator) |
| **Graphiti (per-scope graphs)** — Apache-2.0 (verified in 0001) | $0 licence, but **needs a backing graph DB per scope** (Neo4j Community is **GPL** — verify the store's licence before adopting) + N databases to run, patch, back up | **Defer — the leading candidate.** Best philosophical fit: it *invalidates* superseded edges rather than deleting them, preserving what was known when |
| **Cognee** — Apache-2.0, graph-native, **native MCP** | $0 licence; pluggable storage, but still a new service + store per scope | Defer — lowest friction (our runners are already MCP clients), most complete feature set |
| **Mem0** — Apache-2.0 | $0 licence | **Reject** — no built-in tenant isolation ([0001](0001-context-compression.md)). *Corrected 2026-09-19:* this row said Mem0 removed its graph after losing on its own benchmarks. It did not. Its paper found the graph variant a marginal drop on single-hop, no gain on multi-hop, best on temporal, and ~2% higher overall; Mem0 then replaced its external graph DB with a **native built-in graph**. See *Sources* |
| **One shared graph across all scopes** | $0 licence; **unacceptable governance cost** | **Reject** — see *Because* |
| **Microsoft GraphRAG / LightRAG** | $0 licence; batch re-index cost | Reject for this use — they build graphs from static corpora in a batch pass, not incrementally from agent interactions |
| Build our own | $0 licence; **full maintenance** of a solved problem | Reject — `build-vs-adopt` |

Not adoptable as code, though the ideas are right:
[enterprise-rag-system](https://github.com/rbsundaramoorthy/enterprise-rag-system)
is a portfolio piece (1 star, 81 KB, **no licence at all** — unusable) whose
tenant-isolation and ACL-safe-retrieval design is already our architecture.
[dna.codes](https://dna.codes/) is commercial SaaS; its "living spec — versioned,
owned, always current" framing matches our generated-docs approach.

## Chose

1. **Keep the file-based context hierarchy** for now.
2. **Do not adopt a memory/graph layer yet** — but name the trigger (below) so
   this is a decision, not drift.
3. **If and when we adopt: per-scope graphs, never one shared graph.**
4. **Graphiti is the presumptive choice**, with Cognee the alternative if MCP-native
   integration matters more than temporal fidelity.

## Because

**Our own isolation model is the strongest argument against adopting now.** A
shared knowledge graph is, by construction, a cross-scope leak surface — in direct
tension with the ethical walls, silo dispatch, per-scope mounts and "the vault
never leaves the scrubber" property that this framework exists to enforce.
Per-scope graphs preserve isolation but **erase most of the compounding benefit**,
because context stops being shared at precisely the boundary that makes sharing
valuable. That trade is not resolvable by tooling; it is a governance decision,
and today isolation wins.

**The scale does not justify the cost.** The authoring tax is real, but Atlan's
own framing puts the retrofit window at ~5 agents (build now) versus 30+ (already
paying). We are at the low end with a single operator. Adopting a graph layer
means running and patching a database per scope — and per `dependency-security.md`
we have just spent a week learning what an under-maintained service costs.

**The graph evidence is mixed, and points at graphs without a graph database.**
*(Corrected 2026-09-19 — see the note below.)* Mem0's own paper found its graph
variant helped on temporal and open-domain questions, gave nothing on multi-hop
and slightly less on single-hop, and cost roughly 2× the stored memory and 1.5–1.8×
the total latency, for ~2% overall. The vendor then dropped the *external* graph
database and built the graph into the product. That supports the same direction
as this ADR — keep relationships, don't run a graph store — but it is not
evidence that graphs lose. The *hybrid* framing (vector for recall, graph only
where multi-hop and provenance pay) is unquoted and should be treated as a lead.

> **Correction, 2026-09-19.** This paragraph previously said *"Mem0 deleting its
> own graph after its own benchmarks is the kind of disconfirming finding that
> should outweigh a dozen enthusiastic comparisons"*, and called it the strongest
> signal in the evidence. No source was quoted, and the primary sources say
> otherwise. The decision below does not change: it also rests on isolation,
> scale and operating cost, none of which depended on the Mem0 claim. What
> changes is that the ADR no longer claims evidence that graphs lose.

**What we would be buying, if we bought it:** Graphiti's edge-invalidation model
is the same instinct as our hash-chained ledger — never lose the record of what
was true when. That alignment is why it leads, and why it is worth revisiting
rather than closing.

## Status — revisit when any of these fires

- **A definition demonstrably drifts** between two agents (the silent-failure mode
  actually occurring, not theorised).
- **A second scope needs shared definitions** — i.e. cross-scope context becomes a
  requirement rather than a risk.
- **Agent count passes ~15**, where the authoring tax compounds faster than the
  ops cost of a memory layer.
- **Multi-agent collaboration lands** (see
  [`0004`](0004-human-awareness-observability.md)) — trace IDs and a shared
  knowledge layer are the same problem viewed twice, and should be designed together.

Until one fires, the cheap mitigation is the one we already run: keep definitions
in **generated, single-source artifacts** (compliance map, use-case register,
architecture doc) rather than re-authored prose, so the authoring tax stays near
zero without new infrastructure.

## Sources

Added 2026-09-19 with the correction above. Fetched with `scripts/fetch_source.py`
to `research/raw/`; line numbers refer to those files.

| Source | Line | Says |
|---|---|---|
| `mem0-paper` — arXiv 2504.19413 (Mem0 paper) | 130 | Graph variant scores ~2% higher overall than base Mem0. |
| | 445 | Single-hop: the graph "yields marginal performance drop". |
| | 449 | Multi-hop: the graph "does not provide performance gains". |
| | 453, 457 | Open-domain: graph beats base (75.71 vs 72.93 J). Temporal: graph highest (J 58.13). |
| | 678–679 | Search latency 0.148s → 0.476s; total p50 1.091s, p95 2.590s for the graph variant, vs 0.708s / 1.440s for base. |
| | 685 | Stored memory 7k → 14k tokens per conversation. |
| `mem0-docs-graph` — docs.mem0.ai/platform/features/graph-memory, fetched 2026-09-19 | 117, 128 | Graph memory is built in and always on; it replaced the external graph-store integration (Neo4j and others). |
| | 150 | Credits the graph for gains on multi-hop and temporal questions. |

**Could not confirm:** whether the open-source Mem0 still ships graph memory. The
fetched docs page redirected to the Platform documentation.
