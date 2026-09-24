# Eval / drift lane — continuous eval from the audit ledger

The governance market (Arthur, Fiddler, Galileo) sells **continuous eval + drift**:
quality and task-adherence tracked over time (gap **E2** in MARKET-ANALYSIS.md).
the control plane already logs, per decision, a tamper-evident metadata record. This lane
computes the market's headline metrics **from data we already have** — no new
instrumentation, no prompts/outputs stored, no third-party processor.

`scripts/eval_metrics.py` reads `audit/decisions.jsonl`, and the
`n8n-workflows/eval-drift.workflow.json` lane runs it weekly and reports drift
through the channel-abstracted **Notify** sub-workflow.

## Honest scope — read this first

This is **metadata-derived measurement, not a semantic eval.** The quality signal is
the router's **fresh-context verifier verdict** (`verdict` in the ledger) — a
separate clean-context reviewer's pass/fail (LADDER.md, loop-engineering rule 1). So:

- It measures **what the harness recorded**, not ground-truth correctness. A task the
  verifier passed but that was subtly wrong still counts as "quality held."
- The agentic-leverage number is an **explicitly stated proxy** (below), not a
  claim about shipped business value.
- It is **complementary to — not a replacement for** — semantic / ground-truth
  evaluation (golden sets, human rubric grading, task-outcome tracking). This lane
  is the cheap, always-on first layer over metadata; it does not itself grade
  output correctness. **That semantic-eval gap is now closed by the improvement
  loop** — `scripts/eval_run.py` replays curated + captured regression cases
  through `/route` and grades the actual output (exact/contains/regex/not_contains/not_regex/LLM-as-judge — the negatives exist because most safety regressions are 'must NOT emit X'),
  failing CI on any regression. This lane (online, metadata, always-on) and the
  improvement loop (offline, semantic, gated) are the two halves of eval; see
  **[IMPROVEMENT-LOOP.md](IMPROVEMENT-LOOP.md)**.
- **Semantic drift now has a history too.** This lane trends metadata inside a
  time window; `eval_run.py --record` + `scripts/eval_history.py` trend graded
  OUTPUT across runs, using the same `DRIFT` threshold shape so the two halves
  read alike. Metadata drift says the harness changed shape; semantic drift says
  the answers got worse. They can move independently, and the second is the one
  that matters to whoever receives the work.
- **Two things this lane still cannot do**, both now available offline instead:
  *confidence* (`--judge-votes N` — agreement between independent graders, not a
  self-reported score) and *A/B* (`scripts/eval_ab.py` — paired replay of the
  same cases through two variants, because live traffic-splitting needs volume
  this stack does not have).

Per LADDER.md's measurement-honesty rule: **never gate on raw agent count.** Leverage
is share-of-work-with-the-bar-held, and change-failure must stay flat or fall — both
are trended here, neither is agent-count.

## Metrics

Window is configurable (`--since` / `--days`, default 7) and optionally per-scope
(`--scope`). Shares are over **all attempted decisions in the window** (one
consistent denominator) unless noted.

| Metric | Definition | Meaning |
|---|---|---|
| **Agentic leverage** (headline) | `(verdict==true) OR (no verify required AND not blocked)` / total attempted | Share of attempts produced **with the quality bar held**. Proxy — see below. |
| **Verify-pass rate** | `verdict true / (true + false)` | Quality signal: of the decisions the verifier judged, how many passed. |
| **Change-failure proxy** | `verdict false / (true + false)` | The DORA-style failure signal we track for drift; must stay flat or fall. |
| **Block rate** (by reason) | blocked / total, split by reason | Governance friction / incidents. Reasons: `halted`, `circuit-open`, `approval-required`, `approval-unauthorized`, `guardrail-input`, `mode-forbids-action`, `pii`, `error`. |
| **Guardrail-finding rate** | records with any guardrail finding / total | Injection-shape detections (block or warn). |
| **PII-block rate** | pii-reason blocks / total | Raw-PII prompts refused. (Router throws on a PII block, surfacing as `error:PII…`; the analyzer folds those into the `pii` bucket.) |
| **Approval rate** | `approved==true` / total | Share that carried a human approval. |
| **Risk-level mix** | low / medium / high / critical share | Risk posture over the window. |
| **Tier / model / provider mix** | share per value | Cost proxy (cheaper tier = cheaper spend). |

### The agentic-leverage proxy (stated in every run's output)

> share of attempted decisions produced with the quality bar held =
> `(verdict==true) OR (no verify required AND not blocked)`, over all attempted.

Rationale: a decision "held the bar" if the fresh-context verifier passed it, **or**
if no verification was required and it wasn't blocked (a low-risk completed decision).
Blocked decisions and verifier failures are excluded from the numerator. **Limit:**
`verdict` is a verifier pass, not proof of correctness; "no verify required" trusts
the risk model's decision that a verify wasn't warranted. It is a floor on quality,
not a guarantee.

## Drift

The window is split in half **by time** (first half vs second half); a key metric
moving past a threshold raises a `WARN`. Thresholds live in a documented `DRIFT` dict
at the top of `scripts/eval_metrics.py`:

| Flag | Fires when | Default |
|---|---|---|
| `verify_pass_pct` | verify-pass **drops** more than N pp | 10 pp |
| `change_failure_pct` | change-failure **rises** more than N pp | 10 pp |
| `block_rate_pct` | block rate **multiplies** by > factor (or crosses the floor from ~0) | 2× / 5% floor |
| `critical_risk_share_pct` | critical-risk share **rises** more than N pp | 10 pp |

A half with no data is skipped (can't trend it).

## Running it

```
python scripts/eval_metrics.py                      # last 7 days, text summary
python scripts/eval_metrics.py --days 30 --scope jane
python scripts/eval_metrics.py --since 2026-07-01 --format json
```

- **Chain verify first.** The script re-walks the hash chain with the SAME logic as
  `scripts/verify_audit.py` before computing anything. A broken chain is **REFUSED**
  (`chain_ok:false`, exit 2) — metrics are never computed on data we can't trust.
- **Empty / missing ledger** is handled gracefully ("no data yet").
- `--format json` emits a machine blob (for the n8n lane); `text` (default) is a
  human summary.

### Scheduled lane

`n8n-workflows/eval-drift.workflow.json`: **Schedule Trigger (weekly)** → **Execute
Command** (`python scripts/eval_metrics.py --days 7 --format json /audit/decisions.jsonl`)
→ **Code** node that formats the summary + drift WARNs into
`{ severity, title, message, source }` and hands off to **Notify**. `severity` is
`warn` if any drift flag (or the chain fails to verify), else `info`. Ships
**inactive**. It needs the **live env with the audit volume mounted** where the
script can read it — in the container the ledger is at `/audit/decisions.jsonl`
(adjust the path/working directory for your deploy). Set the Notify workflow ID in
the Notify node. Same Schedule → command → summarize → Notify shape as the
`dependency-bumps` and `retention-sweep` lanes.

## Tests

`tests/eval_metrics_test.py` builds synthetic ledgers in the router's exact
`<hash> <json>` chained format and asserts the computed leverage / verify-pass /
change-failure / block-rate / block-reason / guardrail / PII / approval / risk-mix /
tier-mix against hand calculations, that a deliberately drifting series raises a WARN,
and that a **tampered ledger is refused**. Run:

```
python tests/eval_metrics_test.py
```

## Mapping to the enablement kit (MET-09)

This lane is the enforceable implementation of the enablement kit's **MET-09
(agentic-leverage / continuous-eval metric)**: it operationalizes the LADDER.md
definition — *share of shipped work that is agent-produced with the quality bar held,
change-failure flat or falling; never gated on raw agent count* — as a repeatable
measurement over the audit ledger, with drift alerting. It supplies the **quality
side** of that metric (verifier pass + change-failure); `scripts/gen_status.py` supplies the
spend/volume side (LADDER.md, "Measurement honesty"). Honest caveat carried into the
kit: MET-09 here is a **metadata proxy**; the semantic-eval complement is now
supplied by the improvement loop (offline quality regression, `scripts/eval_run.py`
— see IMPROVEMENT-LOOP.md), so the two lanes together give MET-09 both a cheap
always-on proxy and a gated ground-truth check.
