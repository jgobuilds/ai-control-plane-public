# Build vs adopt: context compression / token reduction

**Date:** 2026-07-22 · **Lens:** `ai-standards/references/build-vs-adopt.md`

## Considered

Maintenance and license figures below are from the GitHub API, not from
rendered pages or search summaries — two summaries in this survey were wrong
(see *Findings*).

| Candidate | Cost | License | Last push | Verdict |
| --- | --- | --- | --- | --- |
| **Headroom** (`headroomlabs-ai/headroom`) | free — Apache-2.0, self-hosted | Apache-2.0 | active (daily) | **Pilot** — see below |
| **Anthropic Context Editing + Memory Tool** (native API) | no new cost — native to the API already in use | n/a — already in stack | n/a | **Adopt** for the Claude lane |
| **OpenLLMetry / Traceloop** | free — Apache-2.0 self-hosted | Apache-2.0, no carve-out | active | **Adopt** for telemetry |
| LLMLingua / LLMLingua-2 | free — MIT | MIT | ~3 months | Reject — see below |
| GPTCache | free — MIT | MIT | **>1 year stale** | Reject — abandoned |
| RouteLLM | free — Apache-2.0 | Apache-2.0 | **Aug 2024** | Reject — dead |
| LiteLLM | free core; **proprietary `enterprise/` tier** | MIT core + **proprietary `enterprise/`** | active | Reject — redundant with our router |
| Langfuse | free core; **proprietary `ee/` tier** | MIT core + **proprietary `ee/`** | active | Reject — prefer OTel-neutral |
| Zep (self-hosted) | **CE discontinued** — cloud-only, metered | — | **CE discontinued Apr 2025** | Reject — cloud-only now |
| Graphiti (Zep's engine) | **CE discontinued** — cloud-only, metered | Apache-2.0 | active | Defer — only if we want a shared knowledge graph |
| Letta (MemGPT) | free — Apache-2.0 | Apache-2.0 | active | Reject — a full agent runtime, wrong shape |
| Mem0 / Cognee | free — Apache-2.0, self-hosted | Apache-2.0 | active | Defer — no built-in tenant isolation |
| context-mode | **ELv2 — not OSI**; commercial terms apply | **ELv2 — not OSI open source** | active | Reject — license + unverifiable claims |
| Build our own compressor | free in licence; **ongoing maintenance cost** | — | — | Reject — solved problem, not a house rule |

## Chose

1. **Adopt Anthropic's native context editing + memory tool** for the Claude lane.
2. **Pilot Headroom** at the runner, deterministic-only, one instance per runner.
3. **Adopt OpenLLMetry** as the telemetry seam on the router.
4. **Build nothing** in this space.

## Because

**The billing model changes the whole calculus.** Runners use subscription /
free-tier auth, not metered per-token API billing. Tools whose value is "cut
your API bill" — semantic caching above all — buy us almost nothing. What
actually binds is **context-window pressure, rate limits, and latency**. Every
adopt/reject above follows from that, and it is the single most important line
in this record: a reader who misses it will re-litigate the whole table.

**Native beats a dependency when it's already there.** Context editing needs no
new package, no new license, and no new trust boundary. Reported ~29% improvement
from context editing alone and ~39% combined with the memory tool, with large
token savings on long-turn benchmarks. It only covers the Claude lane — Gemini
gets nothing — but that is where most of our work runs.

**Headroom is the right shape but not yet a safe default.** Apache-2.0,
local-first, native `headroom wrap claude`, MCP-server mode, and deterministic
JSON/AST compressors that fit our Tier-0-first philosophy. Its own limitations
page is unusually candid. But: created 2026-01-07 (~6 months old), one visible
maintainer, benchmarks entirely self-reported, and a 0→61k-star curve in six
months that resembles at least one project in this same survey whose claims did
not survive checking. **Pilot, measure, then decide** — do not put it in the
request path on trust.

**LLMLingua fails on exactly our payload shape.** Documented: unsuitable for
structured data, and on an agent/tool-use benchmark, compression above ~30% led
to failure in *all* tasks — format survives semantically but breaks structurally.
Our payloads are tool outputs and JSON. Wrong tool for this stack.

**Semantic caching is a security trade we don't need to make.** Cross-tenant
leakage via shared caches is a documented, researched attack class, not a
theoretical one — timing side channels have been shown to reconstruct another
tenant's prompt. Given subscription billing removes most of the upside, we would
be accepting a real isolation risk to buy latency. No.

**Open-core carve-outs.** LiteLLM (`enterprise/`) and Langfuse (`ee/`) are MIT at
the core with proprietary directories — which is why their repo-level SPDX reads
`NOASSERTION`. Legitimate, but it means the useful governance features are the
paid ones. We already have a policy router; a second gateway is redundant weight.

## Conditions on the Headroom pilot

Non-negotiable if it proceeds:

- **One instance per runner. Never shared across scopes.** It sits in the request
  path and sees everything the runner sees. A shared compression service is a
  cross-tenant confluence point — precisely what `CONTEXT-ARCHITECTURE.md`
  exists to prevent. Its TOIN component also learns patterns over time, which is
  per-scope state.
- **Deterministic compressors only; LLMLingua disabled.** Avoids a model call
  inside the runner *and* the HuggingFace egress hole it would open in
  `init-firewall.sh`. A localhost proxy needs no firewall change — the existing
  `-o lo` ACCEPT rule already covers it.
- **Reproduce the benchmarks before trusting them.** The project ships
  `headroom.evals suite`, so the claims are falsifiable. Run them on our own
  workloads — the published numbers are self-reported and unverified by anyone else.
- **Compression happens after PII tokenization**, so it operates on `«PERSON_003»`
  tokens, never raw values. Rehydration must continue to run on model *output*,
  not on compressed input.

## Findings worth keeping

- A search summary claimed GPTCache was actively maintained "as of 2026"; the API
  showed no push in over a year and its README says it stopped adding model
  support. **Verify maintenance from the API, not from summaries or stars.**
- A summary reported Portkey's gateway as Apache-2.0; the API says MIT.
- A widely-shared article frames Headroom as "the Netflix tool." No mention of
  Netflix appears anywhere in the repo or README. Clickbait, not provenance.
- **No package in the memory/context category ships multi-tenant isolation.**
  Mem0, Cognee, and Graphiti all treat it as an application-layer concern. That
  is our problem to own regardless of what we adopt.

## Revisit if

- The Headroom pilot fails to reproduce its compression/accuracy claims on our
  workloads — or succeeds, in which case promote from pilot to adopted.
- Headroom's maintainer count stays at one for another two quarters, or releases
  stall — single-maintainer risk in the request path.
- We move any lane from subscription to metered API billing — that inverts the
  cost calculus and puts caching and compression back on the table.
- We need a shared knowledge graph across agents — re-evaluate Graphiti, and
  budget real time for per-tenant partitioning.
- Anthropic's context-editing betas go GA or change shape.
