# Agent improvement loop — offline quality regression, governed

The eval/drift lane ([EVAL.md](EVAL.md)) measures *metadata the router already
logged* — a cheap, always-on **proxy** for quality. It explicitly does **not**
grade whether an output was actually correct; EVAL.md flags that semantic /
ground-truth gap. **This closes it.**

The agent improvement loop is the **quality-regression counterpart** to the
governance-conformance suite: where `scripts/conformance_test.py` fails CI when a
*governance invariant* regresses, `scripts/eval_run.py` fails CI when *output
quality* regresses against a curated + captured dataset.

It is adapted from LangChain's write-up on **operationalizing the agent improvement
loop** — the production-trace → capture → offline-eval-against-a-regression-dataset
→ fix → prevent-recurrence cycle, with LLM-as-judge for open-ended output. We cite
the concept nominatively; the implementation is our own and is done the Brightworks
way: **governed observability** (below).

## LangChain's loop, mapped to Brightworks components

LangChain frames the loop around *traces / runs / threads / datasets*, *online vs
offline* eval, closing the loop back to a *regression dataset*, and *LLM-as-judge*.
Brightworks already has most of the substrate — this enhancement adds the missing
offline-regression piece and wires it to what exists:

| LangChain concept | Brightworks component |
|---|---|
| Traces / runs (production execution history) | **n8n execution history** + the router **audit ledger** (`audit/decisions.jsonl`) — one tamper-evident, PII-free metadata record per decision |
| Observability / metadata index | The **audit ledger** as the governed metadata index (hash-chained, verified before use) |
| Online eval (quality tracked continuously) | The **eval / drift lane** (`scripts/eval_metrics.py`) — verify-pass / change-failure / drift WARNs from the ledger |
| LLM-as-judge | The router's **fresh-context verifier** (`task_type:review`, independent clean-context reviewer) — reused as the `judge` grader here |
| **Offline eval against a regression dataset** | **THIS** — `scripts/eval_run.py` replays curated + captured cases through `/route` and grades the actual output |
| Capture a failure → regression dataset | `scripts/eval_capture.py` + the `feedback.workflow.json` inbox lane |
| Close the loop / prevent recurrence | Captured case → dataset → `eval_run` as a **CI quality gate** (non-zero exit on any regression) |

So: online eval was already built (metadata proxy); **offline semantic eval is what
this adds**, and the two together are the full loop.

## The differentiator — governed observability

SaaS eval platforms (LangSmith, Arthur, Fiddler, Galileo) are external data
processors: your prompts, outputs, and eval datasets live in *their* cloud. For a
firm under client NDAs that is a non-starter. Brightworks keeps the whole loop
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
[MARKET-ANALYSIS.md](../plans/MARKET-ANALYSIS.md) (row 5: local-first, no SaaS).

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
  eval_run.py --dataset <name>  ⇒ exit 0  (regression prevented; case stays in CI)
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

`eval_run` grades every case and **exits non-zero on any regression**, so it is a CI
quality gate. `--format json` emits a machine blob.

> **Live replay needs the router.** The router has no host port (compose exposes
> nothing outward), so live replay runs from *inside* the compose network —
> `docker compose exec router python /app/scripts/eval_run.py --dataset …` — or
> against a mock. CI and `tests/eval_run_test.py` use `--offline`, which needs no
> router at all.

### 3. Fix, then keep it in CI

Fix the scope's prompt / skill / policy so the case passes, and leave the case in
the dataset. It now guards against that specific regression forever — the "prevent
recurrence" step of the loop.

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

## See also

- [EVAL.md](EVAL.md) — the online eval / drift lane (the metadata-proxy half).
- [GOVERNANCE.md](../security/GOVERNANCE.md) — control-catalog row #13.
- [RETENTION.md](../security/RETENTION.md) — the retention posture datasets inherit.
