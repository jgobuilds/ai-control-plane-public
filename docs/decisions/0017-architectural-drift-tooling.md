# Adopt no architecture-conformance tool; build the router-chokepoint check, because our invariant is a string and not an import

**Date:** 2026-08-08 · **Lens:** `ai-standards/references/build-vs-adopt.md`,
`proving-controls.md`, `change-safety.md`, `cost-awareness.md`,
`dependency-security.md`

## The question

Architectural drift — the gap between the architecture we describe and the one
that runs — is a mature tool category. Before writing anything, the question was
whether to buy: is there a maintained tool that would have caught the drift we
care about?

Three different things travel under that name, and the tooling barely overlaps:

1. **Code ↔ intended structure** (ArchUnit, import-linter, dependency-cruiser) —
   conformance over a module graph.
2. **Infrastructure ↔ declared state** (Conftest/OPA, Checkov, Argo CD) — policy
   over YAML and JSON.
3. **Decay over time** (CodeScene, Arcan) — drift as a trend, read from history.

We already own a hand-built version of (2): `ARCHITECTURE.md` generated from live
containers, `mount_drift_check.py --live` comparing generated against deployed,
`conformance_test.py` asserting the runner map against the compose. Nothing
covered (1).

## Considered

Facts from the **GitHub REST API, PyPI and the npm registry, queried directly on
2026-08-08** — not from search results or memory, because maintenance status is
exactly the claim that goes stale. Repo activity is commits in the trailing 90
days. Alongside that, three measurements of this repo, because the market survey
only matters against what a tool would actually find here:

- **Node: zero parent-relative imports** across router, scrubber, claude-runner
  and gemini-runner. Isolation is the container boundary — a cross-service import
  would not resolve in the image, because the file is not copied in.
- **Python: 78 flat modules**, four inter-directory edge types, exactly one
  questionable edge (`scripts → lanes`, `status_data.py:170`, reading the
  schedule table).
- **The load-bearing invariant is a string.** "Only the router may reach a
  runner" is a URL in an HTTP call. At survey time it appeared in two places
  outside documentation, and **one of them was a violation**.

| Option | Cost | Verdict |
|---|---|---|
| **A. Build a chokepoint check (`scripts/boundary_check.py`)** | $0 licence, no dependency. Non-dollar: ~150 lines plus a test to own, and a text scan that a computed hostname would evade | **Adopt** — it is the only option that can see the invariant at all, and it found a live violation on its first run |
| **B. import-linter** (2.13, released 2026-07-03; pushed 2026-08-07; 10 commits/90d; BSD-2; 7 transitive deps, engine `grimp` has 0) | $0 licence. Non-dollar: 7 deps into CI, a contract file, a CI step — to enforce ~1 rule over flat modules | **Defer** — healthy and well-made, but its edge is transitive layering over a package hierarchy we do not have. Trigger below |
| **C. dependency-cruiser** (18.1.1, 2026-08-02; pushed 2026-08-07; 36 commits/90d; MIT; 18 runtime deps) | $0 licence. Non-dollar: **18 runtime deps** added to a repo whose node services each advertise zero | **Reject** — nothing to enforce. Zero cross-service imports exist by construction, so it would gate a property Docker already guarantees: a green check that protects nothing |
| **D. tach** (0.35.0, 2026-05-12; pushed 2026-06-11; 9 commits/90d; MIT; 8 deps) | Same class as B | **Reject** — same value as import-linter, slower release cadence, no advantage here |
| **E. pytest-archon** (0.0.7, released 2025-09-19; **0 commits in 90 days**) | unpriced — would need a maintenance-risk spike before any adoption | **Reject** — dormant ~11 months. Adopting an unmaintained gate is worse than no gate: it keeps passing |
| **F. ArchUnit** (1.5.0, 2026-08-04; pushed 2026-08-07; Apache-2.0) | n/a | **Reject** — Java only. The category leader, and it cannot run here |
| **G. Deptrac** | n/a | **Reject** — repository **archived** 2025-02-17 (and PHP) |
| **H. madge** (8.0.0, released **2024-08-05**; repo pushed 2026-01-21) | $0 licence; 12 runtime deps | **Reject** — no release in two years, and it visualises rather than gates |
| **I. Conftest / OPA over compose + runner-map** (pushed 2026-08-07 / 2026-08-08; Apache-2.0) | $0 licence. Non-dollar: **Rego as a second policy language** nobody here writes, plus rewriting three working, tested checks | **Reject for now** — the genuine alternative for a greenfield version of this repo. Not worth migrating `conformance_test.py`, `mount_drift_check.py` and `schedule_test.py`, each of which already has a synthesized-failure proof |
| **J. CodeScene / Arcan / Designite** (category 3) | Commercial or research-grade; unpriced — needs a trial to price | **Defer** — trend-over-history is a real gap, but it answers "is this codebase decaying", not "did someone bypass the router". Different question |

## Chose

1. **Buy nothing.** No tool in the conformance category can see this repo's
   load-bearing invariant.
2. **Build `scripts/boundary_check.py`** — two rules, because there are two ways
   to reach a runner: a URL naming a runner host outside the router
   (*addressing*), and an `x-runner-token` header in a send position
   (*credential*). Hosts are discovered from the runner map and the generated
   compose, never hardcoded.
3. **Repoint C2's proof at it**, keeping the older node-aware check in
   `security_test.py` as a companion that can name the offending workflow node.
4. **Defer import-linter with a trigger**, not a vague "later".

## Because

**The category is well-maintained and structurally blind to us.** ArchUnit,
import-linter, dependency-cruiser and tach all reason over module graphs. Our
violation is `http://claude-runner:8080/run` inside an HTTP call — there is no
import edge to forbid. Every one of them would have reported this repo clean
while a shipped example bypassed the router entirely, which is precisely what was
happening.

**The evidence is not hypothetical.** The check's first run found
`n8n-workflow.example.json` at the repo root POSTing straight to a runner with the
runner's own token. C2 was marked closed on 2026-07-28 *with* a proof —
`security_test.py` globs `n8n-workflows/*.json`, and the violation sat one
directory up. The logic was right and the input set was one directory too small:
the fourth control here to pass by looking at the wrong **set** rather than by
being wrong.

**What this gives up, stated plainly:**

- **No transitive import analysis.** If `scripts/` grows a package hierarchy, the
  `scripts → lanes` edge could become a tangle and nothing here would notice.
  That is the deferral in option B, and it has a trigger.
- **A text scan is evadable.** `"http://" + runner_host` defeats it. Accepted:
  the threat model is accident and drift, not a hostile committer — someone
  deliberately obfuscating a bypass has already lost us the review.
- **The send/verify distinction is a whole-file heuristic**, not a parse of every
  n8n node type. Deliberately coarse, because a precise rule nobody can read is
  worse than a coarse one a human reviews. It was nearly got wrong: n8n holds
  `RUNNER_TOKEN` legitimately, to *verify* inbound calls from runners on the
  ask-human door.
  *Superseded 2026-09-17 (threat-model C3).* Holding it to verify was not
  harmless after all. With env access in n8n nodes on, any workflow authored in
  the UI could spend it. The door moved to its own `ASK_HUMAN_TOKEN`, and a third
  rule, HOLDING, now fails any n8n workflow that references `RUNNER_TOKEN` at all.
  The whole-file verify heuristic had also let `seam-heartbeat` *send* the token
  unflagged, because mentioning `$env.RUNNER_TOKEN` counted as verifying.
- **Two checks now cover C2 rather than one.** Redundancy costs maintenance;
  accepted because the node-aware one names the offending node and a text scan
  cannot.

**Adopting B or C anyway would have been the expensive mistake.** Both would have
gone green on the day the real bypass shipped, and a gate that reassures without
protecting is the failure mode this repo keeps finding in its own controls.

## Status

**Implemented.** `scripts/boundary_check.py`, wired into CI and the status page
(9 gates). C2's `asserted_by` now runs it.

Verified, and *how*: `tests/boundary_test.py` synthesises every case rather than
trusting the green run — which matters here because the check went green the
moment the one real violation was deleted, and that is exactly when a broken
check and a working one look identical. A direct URL fails; a token in a send
position fails; the same token in a verify position passes; discovering no hosts
is exit 2 rather than a pass; a stale allowlist entry is exit 2; and the
line-level `boundary-check: describes` marker is proven not to leak to the next
line. 27 python check-scripts and 10 gate scripts pass; CI green on `d350da0`.

Still assumed: that hostname discovery from the runner map and compose stays
complete. If a runner were reachable under a name in neither, the check would not
know to look for it — the "discovered zero hosts is exit 2" guard catches the
total failure, not a partial one.

**Revisit when** any of these becomes true:

- `scripts/` is reorganised into packages (Part C of the parked scripts-reorg
  plan) — **adopt import-linter** then; layering contracts earn their keep the
  moment there are layers.
- A node service gains a second internal module or a shared package — then
  reconsider dependency-cruiser, which today has nothing to bite on.
- A third check would benefit from policy-as-data over compose/JSON — reconsider
  Conftest, since the migration cost amortises differently at three than at one.
