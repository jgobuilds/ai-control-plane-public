# Agent improvement loop — offline quality regression, governed

The eval/drift lane ([EVAL.md](EVAL.md)) measures *metadata the router already
logged* — a cheap, always-on **proxy** for quality. It explicitly does **not**
grade whether an output was actually correct; EVAL.md flags that semantic /
ground-truth gap. **This closes it.**

The agent improvement loop is the **quality-regression counterpart** to the
governance-conformance suite: where `scripts/conformance_test.py` fails CI when a
*governance invariant* regresses, `scripts/eval_run.py` exits non-zero when *output
quality* regresses against a curated + captured dataset — host-side, where the
per-scope datasets live; CI only proves the grader (see step 2).

It is adapted from LangChain's write-up on **operationalizing the agent improvement
loop** — the production-trace → capture → offline-eval-against-a-regression-dataset
→ fix → prevent-recurrence cycle, with LLM-as-judge for open-ended output. We cite
the concept nominatively; the implementation is our own and is done the the control plane
way: **governed observability** (below).

## LangChain's loop, mapped to the control plane components

LangChain frames the loop around *traces / runs / threads / datasets*, *online vs
offline* eval, closing the loop back to a *regression dataset*, and *LLM-as-judge*.
the control plane already has most of the substrate — this enhancement adds the missing
offline-regression piece and wires it to what exists:

| LangChain concept | the control plane component |
|---|---|
| Traces / runs (production execution history) | **n8n execution history** + the router **audit ledger** (`audit/decisions.jsonl`) — one tamper-evident, PII-free metadata record per decision |
| Observability / metadata index | The **audit ledger** as the governed metadata index (hash-chained, verified before use) |
| Online eval (quality tracked continuously) | The **eval / drift lane** (`scripts/eval_metrics.py`) — verify-pass / change-failure / drift WARNs from the ledger |
| LLM-as-judge | The router's **fresh-context verifier** (`task_type:review`, independent clean-context reviewer) — reused as the `judge` grader here |
| **Offline eval against a regression dataset** | **THIS** — `scripts/eval_run.py` replays curated + captured cases through `/route` and grades the actual output |
| Capture a failure → regression dataset | `scripts/eval_capture.py` + the `feedback.workflow.json` inbox lane |
| Close the loop / prevent recurrence | Captured case → dataset → `eval_run` as a **quality gate** (non-zero exit on any regression), run host-side where the dataset lives |

So: online eval was already built (metadata proxy); **offline semantic eval is what
this adds**, and the two together are the full loop.

## The differentiator — governed observability

SaaS eval platforms (LangSmith, Arthur, Fiddler, Galileo) are external data
processors: your prompts, outputs, and eval datasets live in *their* cloud. For a
firm under tenant NDAs that is a non-starter. the control plane keeps the whole loop
**local and governed**:

- **Per-scope.** Eval data lives under `eval/datasets/<name>/` and every case
  carries a `scope`; the boundary is the same governance boundary the router
  enforces. Cases are never pooled cross-scope.
- **PII-tokenized.** `eval_capture` POSTs the prompt to the scrubber `/scrub`
  endpoint *before* writing, so a stored case holds `«EMAIL_1»`, never raw PII —
  the same deterministic vault the ingest path uses. A dataset is safe to keep
  around precisely because it was tokenized on capture.
- **Retention-bound + local.** Datasets, traces, and the feedback inbox live on the
  deploy volume and are `.gitignored` (only the safe `eval/datasets/starter/` suite
  is committed) — subject to the same retention posture as other scope data
  ([RETENTION.md](../security/RETENTION.md)), never shipped to a third party.
- **Never cross-scope.** No case, output, or judge call leaves its scope; the judge
  is the same-stack router, not an external grader.

That stance — a full production-trace → offline-regression → fix loop with **no
external data processor** — is the improvement-loop analogue of the market edge in
`MARKET-ANALYSIS.md` (internal) (row 5: local-first, no SaaS).

## The concrete workflow

```
  prod failure (a human thumbs-down on an agent output)
        │
        ▼
  feedback.workflow.json  ──►  eval/inbox/<id>.json      (n8n; ./eval mounted)
        │
        ▼
  eval_capture.py --ingest eval/inbox --scrubber-url …   (prompt TOKENIZED here)
        │                                                  writes a case:
        ▼                                                  eval/datasets/<name>/<id>.json
  eval_run.py --dataset <name>                            (replay + grade)
        │                                                  exit != 0  ⇒  fix it
        ▼
  fix the scope's prompt / skill / policy
        │
        ▼
  eval_run.py --dataset <name>  ⇒ exit 0  (regression prevented; case stays in the dataset)
```

### 1. Capture (close the loop)

Direct (one case), or by promoting the feedback inbox:

```bash
# direct — capture a known prod failure, tokenizing the prompt
python scripts/eval_capture.py --dataset acme --scope acme \
    --prompt @/path/to/failing_prompt.txt --task-type plan \
    --grader-type judge --goal "Plan must include an explicit rollback step" \
    --source prod-failure \
    --scrubber-url http://scrubber:8080 --scrubber-token "$SCRUBBER_TOKEN"

# ingest — promote thumbs-downs the feedback workflow dropped into eval/inbox/
python scripts/eval_capture.py --dataset acme --ingest eval/inbox \
    --scrubber-url http://scrubber:8080 --scrubber-token "$SCRUBBER_TOKEN"
```

A case is `{ id, scope, task_type, action, prompt, grader, source, created }`. The
grader is one of:

| grader | pass when | use for |
|---|---|---|
| `exact` | output `==` `value` (whitespace-trimmed) | deterministic single-token output |
| `contains` | `value` is a substring (case-insensitive) | a required label / keyword |
| `regex` | `value` matches | structured output shape |
| `judge` | the router (fresh-context reviewer) says the output meets `goal` | open-ended output (plans, prose) |

The graders are **pure, importable functions** (`grade_exact`, `grade_contains`,
`grade_regex`, `grade_judge`, dispatched by `grade(output, grader, judge_fn)`), so
they're unit-tested directly and reusable by any caller.

### 2. Offline eval (the gate)

```bash
# live replay through the router (needs the stack — see the note)
python scripts/eval_run.py --dataset acme

# offline — grade PRE-CAPTURED { case_id, output } outputs; NO live stack (CI)
python scripts/eval_run.py --dataset acme --offline outputs.jsonl
```

`eval_run` grades every case and **exits non-zero on any regression**, so it is a
quality gate — but **not a CI one for real data**: datasets are gitignored per scope,
so CI never sees them. CI runs `tests/eval_run_test.py`, which proves the grader
grades; the gate over a real dataset runs host-side, where the data is. Until
2026-09-19 this paragraph called it a CI gate while nothing ran even the test
(ADR 0020). `--format json` emits a machine blob.

> **Live replay needs the router.** The router has no host port (compose exposes
> nothing outward), so live replay runs from *inside* the compose network —
> `docker compose exec router python /app/scripts/eval_run.py --dataset …` — or
> against a mock. CI and `tests/eval_run_test.py` use `--offline`, which needs no
> router at all.

### 3. Fix, then keep it in the dataset

Fix the scope's prompt / skill / policy so the case passes, and leave the case in
the dataset. It now guards against that specific regression forever — the "prevent
recurrence" step of the loop.

### 4. Record it, so quality has a history

```bash
python scripts/eval_run.py --dataset acme --offline outputs.jsonl --record --label baseline
python scripts/eval_history.py --fail-on-drift
```

Every run used to print a verdict and vanish, so the semantic half of eval
trended **nothing** — "is quality drifting?" could only be answered about
metadata. `--record` appends one line per run to `eval/results.jsonl`
(gitignored: it carries scope names, and it is local measurement data, not
governance evidence — no hash chain, because that would imply a threat model
that does not apply).

`eval_history.py` reads it and reports three things:

| Output | What it tells you |
|---|---|
| **TREND** | pass rate and mean confidence per run, newest last |
| **DRIFT** | the latest run vs a baseline of the runs before it; thresholds mirror `eval_metrics.py`'s `DRIFT` dict so both halves speak the same language |
| **BROKE / fixed** | cases that changed verdict since the previous run |

The third one earns its place: a pass rate can hold steady while two cases swap
pass and fail, and the aggregate cannot show you that. Comparison is always
within a `(dataset, label)` pair — comparing a `t3` variant against a `t1`
baseline and calling the difference "drift" would be a category error. That is an
A/B, below.

### 5. Confidence — agreement, not self-report

```bash
python scripts/eval_run.py --dataset acme --judge-votes 3
```

Runs N independent judges per judge-graded case; **confidence is their
agreement**, and majority decides the verdict. The obvious alternative — asking
the model how confident it is — yields a number uncorrelated with correctness
that *looks* like a measurement, which is worse than having none. Three fresh
graders splitting 2-1 is an observation: the case is genuinely ambiguous against
its stated goal, whichever way the majority fell. `lowConfidence` in the result
lists exactly those cases.

Deterministic graders ignore `--judge-votes` and report confidence `1.0`; a
**single** judge reports `null`, not `1.0`, because pretending one
non-deterministic grader is certain erases the distinction the feature exists to
draw. Use an odd N — an even panel can tie, and a tie fails.

### 5b. Resample — is the AGENT stable?

```bash
python scripts/eval_run.py --dataset acme --resample 3
```

`--judge-votes` asks whether the GRADERS agree about one output. `--resample`
asks whether the AGENT produces a gradeable output twice running — a different
failure, and the two were being conflated. A case that passes 3 times in 5 is not
a case that passes, and until this existed an A/B would charge that noise to
whichever variant drew the bad sample.

Majority decides the verdict; `stability` is the majority share, so 1.0 is
deterministic and 0.6 is 3-of-5. Flapping cases are listed in `unstable` and
marked `← FLAPPING` in the text output. **Fix those before trusting an A/B** — an
unstable case is a finding about the case or the prompt, not a result.

Live replay only. Grading one captured output N times measures nothing and would
bill N judge calls to say so, so `--offline` clamps it to a single run and says
why. (Idea taken from AgentLens's turn-level resampling — see the dossier
landscape; the harness itself was declined as a peer to the runner.)

### 6. A/B — paired replay, not a traffic split

```bash
python scripts/eval_ab.py --dataset acme --a '{}' --b '{"tier":"t3"}' \
    --a-label t2 --b-label t3 --judge-votes 3
```

A variant is a set of **request overrides** merged into each replay, honoured
only when `policy.controls.allowRequestOverride` is true — so running the
experiment is something policy grants, not something the script takes.

**Why paired.** Tagging live traffic A/B needs volume this stack does not have;
at these rates a split would take months to separate a real effect from noise,
and an underpowered A/B that announces a winner launders noise into a decision.
Same cases through both variants removes between-group variance entirely.

**The statistic.** Only *discordant* pairs — cases where the variants disagree —
carry information. That is McNemar's test, and at these counts the exact binomial
is the honest form: no normal approximation, no chi-square at n=4. Below
`MIN_DISCORDANT` the tool says **NOT CONCLUSIVE** and refuses to name a winner,
even when the pass rates differ visibly. `tests/eval_analysis_test.py` asserts
that floor cannot hide a significant result.

## Case provenance — captured vs curated

The `source` field is not decoration. Two kinds of case live in these datasets
and they are worth very different amounts:

- **captured** — a real production failure, recorded at the time through
  `eval_capture.py`. The gold standard: it is evidence that the failure happened,
  so the case cannot be argued away.
- **curated** — written from a documented rule, in advance of any failure. Useful
  and legitimate, but it encodes what someone *believes* the system should do.
  A curated case can be wrong in a way a captured one cannot.

Record which. `source: "curated:<doc> <rule>"` names the rule the case guards, so
a future reader can check the case against its source instead of guessing at
intent.

**The audit ledger cannot be mined for captured cases.** It stores
`promptSha256`, never the prompt — deliberately, and that is not going to change.
So capture has to happen at the moment of failure, through the capture path.
There is no retrospective route.

### Writing a case that can actually fail

Two rules, both learned the hard way:

- **Give every case a bad transcript and confirm it FAILS.** A case that passes
  everything measures nothing. The deterministic graders make this cheap: write
  one output that should pass and one that should fail, run both `--offline`.
- **Negative assertions need output to assert against.** `not_contains` /
  `not_regex` pass on an empty string, because absence of a forbidden thing is
  trivially true of nothing. They now fail on empty output for exactly this
  reason — the first live run of a curated set reported PASS on a case where the
  runner had returned nothing at all (see D1 in the threat model).

## Honest limits

- **Offline eval quality is only as good as the grader.** A weak `contains` value or
  a vague judge `goal` gives a weak signal. Datasets are a curated asset, not a
  free lunch.
- **Judge graders are non-deterministic.** LLM-as-judge (`grade_judge`) can disagree
  with itself run-to-run; prefer `exact`/`contains`/`regex` where the expected
  output is deterministic, and reserve `judge` for genuinely open-ended output.
- **Live replay needs the live router** (and its runners / subscriptions). The
  always-runnable path is `--offline` over captured outputs; that is what CI uses.
- **This is a pre-ship quality gate, not a runtime enforcement control.** Runtime
  governance stays with the router (risk / mode / guardrails / audit); this grades
  quality *offline*, complementing — not replacing — the online eval/drift lane.
- **Everything here measures the golden set, so the golden set is the ceiling.**
  `eval/datasets/starter/` has three cases. Drift, confidence and A/B will all
  run happily against three cases and report confidently on nothing — the A/B
  will say NOT CONCLUSIVE, but the drift check will not tell you its baseline is
  too thin to mean much. Growing the dataset from captured production failures
  (`eval_capture.py`) is the unglamorous prerequisite, and it is the part that
  cannot be automated.
- **The audit ledger cannot seed cases.** It stores `promptSha256`, never the
  prompt — deliberately. So there is no mining real traffic into a dataset after
  the fact; capture has to happen at the time, through the capture path.
- **Confidence is agreement between graders, not calibration.** A panel that is
  unanimously wrong reports 1.0. It measures ambiguity against the stated goal,
  which is a different thing from correctness, and a vague `goal` makes it
  meaningless in a way no number here will reveal.

## See also

- [EVAL.md](EVAL.md) — the online eval / drift lane (the metadata-proxy half).
- [GOVERNANCE.md](../security/GOVERNANCE.md) — control-catalog row #13.
- [RETENTION.md](../security/RETENTION.md) — the retention posture datasets inherit.
