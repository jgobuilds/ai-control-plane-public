# Evolve skills, wikis and the knowledge graph from evidence — gated by paired eval, dataset first, proposer last

**Date:** 2026-09-19 · **Lens:** `ai-standards/references/proving-controls.md`,
`knowledge-for-agents.md`, `build-vs-adopt.md`, `cost-awareness.md`,
`evidence-standard.md`

## Context

Prompted by WikiSkill (Google Research, arXiv 2608.27454v1, 2026-08-27), fetched to
`research/raw/arxiv-2608-27454.txt` and quoted by line under *Sources*. Its loop has
four parts. The task agent runs with the current skills and writes **immutable
traces** (l. 245). A maintainer agent consolidates sampled traces into a **wiki of
failure and success patterns**, with an evolution log and a skill-impact tracker
(l. 249). A proposer makes **one atomic skill edit** per iteration (l. 287), and
each skill carries a `PURPOSE.md` mapping it to the **patterns that motivated it**
(l. 253). The edit is kept only if the validation score beats the best so far;
otherwise it is **rolled back** (l. 291).

Three findings bear on design, not only on results. How much weight each carries
is set out in *Evidence behind each decision* below.

- **Keep the wiki away from the agent doing the work.** In one ablation, on one
  model (Gemini-3.5-Flash), giving the task agent wiki access during training runs
  cut average accuracy from 63.7% to 60.9% (l. 740, 799). No significance test is
  reported, and the authors offer the cause as a hypothesis.
- **Skills can transfer badly between models.** One skill set evolved on a small
  model cut Gemini's score on a spreadsheet benchmark from 50.5% to 18.1% (l. 730).
- **Small validation sets make the gate noisy.** The authors say so (l. 1723), and
  compensate by running the whole pipeline three times with paired bootstrap tests
  (l. 536).

What this repo already has, checked on 2026-09-19 rather than recalled:

| WikiSkill stage | Here | Gap |
|---|---|---|
| Raw traces | Hash-chained ledger, immutable | Metadata only, PII-free by design: no reasoning, no tool calls. `eval_capture.py` stores scrubbed prompts. |
| Pattern wiki | Recurrence register, 8 known-cause signatures, `docs/solutions/` | About the **harness's** failures (CI, lanes), not agents' task outcomes. `ci_diagnose` writing signatures back is the only automated maintainer. |
| Skills + provenance | Scope `skills/` mounted into runners; `promote-skill` workflow (scrub → review → commit) | No link from a skill to its evidence. `promote-skill` is loaded and has never run. |
| Graph | IDs and cross-links per `knowledge-for-agents.md` §5; graph DB **deferred** by [ADR 0008](0008-context-and-knowledge-at-scale.md) | No IDs across skills, patterns and evidence, so no provenance graph exists. |
| Proposer | — | None. |
| Gate + rollback | `eval_run.py` (grades cases); `eval_ab.py` (paired, exact two-sided McNemar, **≥ 6 discordant pairs** before any verdict) | `eval_ab` variants are request overrides (tier, model, verify), not skill sets. The only committed dataset has **3 cases**. |

**A correction found while checking.** `IMPROVEMENT-LOOP.md` described `eval_run` as a
CI quality gate. Nothing ran it: no workflow step, and `tests/eval_run_test.py`
does not match the `test_*.py` pattern the quality job discovers. The test passes
and is now wired into `quality`. The gate over real data cannot run in CI at all,
because datasets are gitignored per scope (`.gitignore`, `eval/datasets/**`). It
has to run where the data is, like the recurrence counter.

## Considered

| Option | Cost | Verdict |
|---|---|---|
| **A. Adopt WikiSkill's implementation** | unpriced: the fetched paper names no code release, so there is nothing to adopt; pricing it needs one | **Defer.** Revisit if code ships; the design is borrowable now. |
| **B. Adopt a baseline tool** (EvoSkill, SkillOpt, Trace2Skill, the paper's comparators) | unpriced: licences, maintenance and fit not checked; the paper reports WikiSkill beating all three | **Defer.** Survey only if option C's proposer stage fires and a build looks larger than an adopt. |
| **C. Build the full autonomous loop now** — maintainer, proposer, auto-accept on "beats best" | **Metered** (Agent SDK credit at API rates, requests stop at the ceiling): each iteration replays train and validation cases plus two agent calls, and the paper runs three full pipelines to get a stable answer. Non-dollar: full-trace storage, which the PII-free ledger was designed to avoid; auto-accept removes the human gate on a behaviour change | **Reject.** A 3-case validation set turns "beats best" into a noise detector, and an automated behaviour change without a human gate contradicts the design constraint that the gate is the product. |
| **D. Staged: evidence first, provenance graph, paired skill-set eval, proposer that only proposes** | $0 licence for stages 0–3. Metered from stage 3: one `eval_ab` run ≈ 2 × cases × (1 + judge calls). Non-dollar: authoring eval cases, a runner contract change to mount an alternate skill set, a pattern store per scope, human review of every proposal | **Adopt.** |
| **E. Leave skills and wikis hand-maintained, no provenance** | $0. Non-dollar: skills drift silently; nobody can say why a skill exists or whether it still helps | **Reject.** The same failure the retracted-claims check addresses for docs, one layer down. |
| **F. Adopt a graph database for the provenance graph** (Graphiti / Cognee per ADR 0008) | $0 licence; a store per scope to run, patch and back up | **Reject now.** ADR 0008's triggers have not fired, and IDs + links is a graph at zero infrastructure (`knowledge-for-agents.md` §5). |

## Chose

**One model for all three artefact types.** Evidence becomes patterns (the wiki),
patterns motivate skills, and every link is an ID. Each change is atomic, cites its
evidence, passes a paired eval where it can be evaluated, and is approved by a
human before it is live.

1. **Stage 0: make the gate honest (done with this ADR).** `tests/eval_run_test.py`
   runs in `quality`. `IMPROVEMENT-LOOP.md` now says what runs where: the grader is
   tested in CI; the gate over real data runs host-side.
2. **Stage 1: evidence per scope.** Grow each scope's eval set from real captured
   failures through `eval_capture.py` (scrubbed, per-scope, retention-bound).
   Prefer deterministic graders (`exact`, `contains`, `regex`) over `judge`, so the
   gate isn't grading with a model it is also tuning. A scope is **eval-ready** at
   ≥ 30 cases: small enough to reach, and large enough that 6 discordant pairs is a
   plausible outcome of a real skill change rather than a lucky one.
3. **Stage 2: the provenance graph, as IDs and links.**
   - Namespaces: `eval:<dataset>/<id>`, `ledger:<hash>`, `sol:<slug>`,
     `rec:<match>`, `pat:<scope>/<slug>`, `skill:<scope>/<name>`.
   - **Wiki:** a pattern page (`<overlay>/workspace/<scope>/wiki/patterns/<slug>.md`)
     carries frontmatter `id`, `evidence: [ids]`, `last_verified`, and is marked
     superseded, not deleted, when evidence stops supporting it.
   - **Skills:** each skill carries the `PURPOSE.md` equivalent as frontmatter:
     `motivated_by: [pat:…]`, `proven_on: {model, version, eval: <result id>}`.
   - A build check resolves every reference and **fails on a dangling ID** or on a
     **cross-scope link**: a skill in scope A citing a pattern from scope B is the
     ethical wall broken in the graph. Moving a pattern into a shared scope goes
     through `promote-skill`'s scrub → review → commit, never a link.
   - The index (ID → file, anchor, summary) is generated, never hand-written.
4. **Stage 2 also: the wiki is never mounted into a runner.** Runners get `skills/`
   and context; `wiki/` stays in the overlay for the proposer and humans. This
   follows the paper's ablation and our own isolation model. A conformance
   assertion alongside `mount_drift_check.py` makes it a control, not a convention.
5. **Stage 3: skill sets become an `eval_ab` variant.** A variant can name an
   alternate skill directory; the runner mounts it for that replay only. A skill
   change is accepted only on a conclusive paired win (≥ 6 discordant pairs,
   p < 0.05) **and** human approval via `promote-skill`. Inconclusive means not
   accepted, never "no worse, so fine".
6. **Stage 4: a proposer lane that proposes and never applies.** A scheduled lane
   reads the scope's wiki and recent failures, writes pattern updates and **one**
   skill edit as a proposal with its evidence IDs, and runs stage 3's eval. The
   outcome goes to `promote-skill` for a human either way, and the skill-impact
   record is written whether it is accepted or not, because the rejections are
   what the next proposal learns from. As in the paper (l. 298), a rejected skill
   edit rolls back; the pattern updates and the impact record do not.
7. **Skills are proven per model.** `proven_on` gates dispatch: a skill proven on
   one vendor is not used on the other vendor's runner until it has its own eval
   there.

## Because

WikiSkill's contribution is not the agents, it's the **separation of evidence,
knowledge and executable artefact, and the links between them**. That part is cheap,
fits what we already believe (`knowledge-for-agents.md`: IDs and links are a graph;
ADR 0008: per-scope, never shared), and pays off before any model is added to the
loop: a human editing a skill can see why it exists and whether it still earns its
place.

The autonomy is the expensive, risky part, and our situation is the opposite of the
paper's. It had benchmarks with fixed train/validation/test splits and still needed
three full runs to trust a result. We have one operator, low volume, and 3 committed
cases. The gate is only as good as the dataset, so the dataset comes first and the
proposer comes last.

**What this gives up:** speed. A human approves every skill change, so throughput is
bounded by review, and a real improvement can sit in an inconclusive state until
enough cases accumulate. The paper accepts on "beats best" without a significance
floor; we deliberately won't, and will accept fewer changes as a result.

**What it changes about ADR 0008:** nothing yet. The provenance graph is exactly the
file-based graph 0008 chose to keep. If stage 4 runs across several scopes and
pattern pages start needing multi-hop queries, that is new evidence for 0008's
triggers, not a reason to bypass them.

**Where this could be wrong:** deterministic graders may not fit open-ended scopes,
forcing reliance on `judge`, and a model judging model-tuned skills can reward its
own biases. If that happens, stage 3 needs a second-vendor judge, not a
same-vendor one.

## Evidence behind each decision

Rated 2026-09-19. **Measured here** means our own data; **research** means quoted
from a source under *Sources*; **reasoning** means no performance evidence either
way. Nothing in this ADR has been measured by us as an improvement.

Two caveats apply to every research row: WikiSkill is **one preprint** (v1, not
peer-reviewed), and its authors ran their own benchmarks, including on their
employer's models — the same kind of evidence discounted for supermemory in
`idea-dossier/02-landscape.md`.

| Decision | Evidence | Strength |
|---|---|---|
| Dataset before any automated gate | **Research:** small validation sets add gate noise (l. 1723); the authors needed 3 full runs and paired bootstrap tests (l. 536). **Measured here:** `eval/results.jsonl` on the live checkout holds 4 recorded offline runs of the 3-case starter set, all on commit `058e934`, with pass rates of 0%, 67%, 67% and 33%; `starter-classify-sentiment` both passed and failed. | **Strongest.** The measurement suggests large run-to-run swings at n = 3, but the runs do not record why they differed, so it is suggestive, not proof. |
| A human approves every change | None on performance. It is the design constraint that the gate is the product, and it costs throughput. | A deliberate trade-off, not an optimisation. |
| Paired McNemar gate, ≥ 6 discordant pairs | Arithmetic: with 5 discordant pairs, even 5–0 gives two-sided p = 0.0625. | Correct as a floor; says nothing about whether we reach it. |
| 30 cases per scope | None. | An estimate, to be tested by stage 3's inconclusive rate. |
| Deterministic graders over `judge` | Reasoning: don't grade with the model being tuned. | Unmeasured. |
| Keep a pattern wiki | **Research:** one ablation, one model: proposer with the wiki 48.7% → 63.7% (l. 795). | Large effect, single source. |
| Never mount the wiki into a runner | **Research:** same ablation: 63.7% → 60.9% with wiki access for the task agent (l. 799). No significance reported. | **Weak.** Adopted because it is also what our isolation model wants and costs nothing; not because it is shown. |
| Prove skills per model before cross-vendor use | **Research:** negative transfer observed, 50.5% → 18.1% (l. 730). | A real failure, so good as a risk signal; how often it happens is unknown. |
| Provenance links, skill → pattern → evidence | None. The paper uses `PURPOSE.md` but does not test whether it helps. | Governance and maintainability reasoning. |
| IDs + links, no graph database | **Research, indirect:** Mem0 replaced its external graph DB with a built-in graph, and its paper found the graph worth ~2% overall at 2× memory. That argues for relationships without a store, not against graphs. See the 2026-09-19 correction to [ADR 0008](0008-context-and-knowledge-at-scale.md). | Weak but consistent. The ADR 0008 claim that "graphs lose" was wrong and has been retracted. |

**What would turn this into proof, cheapest first:** measure run-to-run noise on the
starter set (about 10 replays on one commit, recording why results differ); then,
once a scope has 30 cases, run our own version of the wiki-access ablation before
relying on the paper's.

## Status

**Stage 0 implemented** (this change): `tests/eval_run_test.py` runs in `quality`,
and `IMPROVEMENT-LOOP.md` is corrected. Before wiring it, a mutation (every
`contains` case passes via `grade()`) **survived** the test: it called each grader
directly, never through the dispatch a real run uses. It now checks all five
deterministic graders through `grade()`, pass and fail, and was watched failing on
that mutation. Stages 1–4 are **decided, not built**.

Verified: the paper's claims quoted by line from a raw fetch (2026-09-19);
`eval_ab.py`'s `MIN_DISCORDANT = 6` and two-sided exact test; `eval_run` absent from
every workflow before this change; `promote-skill` loaded and idle (generated
`ARCHITECTURE.md`). Assumed: that 30 cases per scope is enough to see a real effect.
That is an estimate, and stage 3's inconclusive rate will test it.

**Revisit when:**
- a scope reaches **30 eval cases** → build stage 2 in that scope, then stage 3;
- stage 3 has **accepted or rejected 5 skill changes by hand** → consider stage 4;
- WikiSkill **releases code** → re-price option A;
- a pattern needs to be **shared across scopes** → that is ADR 0008's second trigger.

## Sources

`research/raw/arxiv-2608-27454.txt`, fetched 2026-09-19 with
`scripts/fetch_source.py get https://arxiv.org/html/2608.27454`. Line numbers refer
to that file.

| Line | Claim |
|---|---|
| 245 | Raw layer: full step-by-step traces; immutable. |
| 249 | Wiki layer: pattern pages plus an evolution log and a skill-impact tracker, so rejected interventions are not proposed again. |
| 253 | Skills carry `PURPOSE.md` mapping each skill to its motivating wiki patterns. |
| 257 | The loop's four components; the task agent is restricted from the wiki during training. |
| 287 | One atomic proposal per iteration, targeting a single skill. |
| 291 | Accept if validation beats the best score, otherwise keep the previous skill set. |
| 298 | A rejected proposal reverts the skills; the wiki is never rolled back; each outcome is appended to `skill-impact.md`. |
| 536 | Three independent runs; paired bootstrap significance at p < 0.05. |
| 730 | Negative cross-model transfer (spreadsheet: 50.5% → 18.1%). |
| 740 | The ablation uses one model, Gemini-3.5-Flash, across four wiki-access configurations. |
| 795 | Proposer with persistent wiki: 48.7% → 63.7% average. |
| 799 | Wiki access for the task agent during training: 63.7% → 60.9% average; the cause is given as a hypothesis. |
| 1723 | Small validation splits introduce noise into the gating decision. |
