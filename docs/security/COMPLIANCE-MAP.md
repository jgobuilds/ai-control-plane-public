# Framework-coverage matrix — the compliance mapping

Brightworks maps **every governance control** (the [GOVERNANCE.md](GOVERNANCE.md)
control catalog) to the recognized AI-governance frameworks, and calls out the
gaps honestly. This is the executive / consulting artifact auditors and
prospects ask for: *"show me how your controls line up against OWASP / NIST /
the EU AI Act."*

It maps three frameworks:

| Framework | Scope of the mapping | Version / dating |
|---|---|---|
| **OWASP Agentic AI — Threats & Mitigations** ("Agentic Top 10") | The agentic-AI threat categories (T1 memory poisoning, T2 tool misuse, T3 privilege compromise, T5 cascading hallucination, …) | OWASP GenAI Security Project taxonomy, v1.0 (Feb 2025) + 2026 drafts. OWASP has **not** frozen an officially numbered "Top 10 for Agentic AI"; numbers here are indicative, threats are labelled by name. Reviewed 2026-07-18. |
| **NIST AI RMF** | The four functions — Govern, Map, Measure, Manage | AI RMF 1.0 (NIST AI 100-1, Jan 2023) + Generative AI Profile (NIST-AI-600-1, Jul 2024). Reviewed 2026-07-18. |
| **EU AI Act** | High-risk-system obligations (Arts 9–15) + post-market monitoring / incident reporting (Arts 72–73) | Regulation (EU) 2024/1689, OJ 2024-07-12. Applicability depends on the deployer's own risk class — this is a control checklist, not a legal determination. Reviewed 2026-07-18. |

Framework names are cited **nominatively**; requirement text is **summarized in
our own words**, never reproduced verbatim. Where a framework's numbering or
wording varies by version, the version and review date are recorded above and in
`compliance/framework-map.yaml`.

## How to regenerate

The matrix is generated from one hand-authored source of truth:

```
python scripts/gen_compliance_map.py
```

| File | Role |
|---|---|
| `compliance/framework-map.yaml` | **Source of truth** — hand-authored control→framework→requirement→coverage entries + the curated gap list. Edit this. |
| `scripts/gen_compliance_map.py` | Generator (stdlib + pyyaml). Reads the yaml, emits the doc. |
| `docs/compliance-map.md` | **Generated** — three matrices (one per framework), per-framework coverage summaries, and the honest gap list. Do not edit by hand. |
| `tests/compliance_test.py` | Validates the yaml is well-formed, every referenced control name matches a real GOVERNANCE.md control, and the generator produces the doc. |

`tests/compliance_test.py` runs standalone (`python tests/compliance_test.py`)
and does not touch `scripts/conformance_test.py` — the two are independent.

## The honest gap list — what Brightworks does NOT yet cover

Coverage is deliberately three-valued (`full` / `partial` / `none`). The gaps
below are cross-referenced to the **E2 / E3 roadmap** in
[MARKET-ANALYSIS.md](../plans/MARKET-ANALYSIS.md) and the [THREAT-MODEL.md](THREAT-MODEL.md)
findings. (The full per-requirement breakdown lives in `docs/compliance-map.md`.)

| Gap | Frameworks touched | Roadmap / ref |
|---|---|---|
| **Continuous model evaluation, drift & task-adherence over time** | NIST Measure, EU Art 15, OWASP T5 (partial) | **E2** (continuous eval + drift) |
| **Bias / fairness testing** of agent outputs | NIST Measure, EU Art 10/15 | backlog |
| **Model cards / full technical-documentation file** (e.g. EU Annex IV) | EU Art 11, NIST Map | backlog (compliance-evidence exporter) |
| **Serious-incident reporting** to stakeholders/authorities | EU Art 72–73, NIST Manage | backlog (compliance-evidence exporter) |
| **ML-based injection / PII detection** (heuristic + US-regex only today) | OWASP T1/T6/T15, EU Art 10/15 | **E3**; threat-model M2, H3 |
| **Runtime resource limits** (CPU/mem/pids) | OWASP T4 | threat-model M7 |
| **Authenticated approval ingress / strong approver identity** | OWASP T9, EU Art 14 | threat-model M5 (RBAC.md) |
| **Structural pool-mode isolation** (advisory in pool, structural only in silo) | OWASP T1/T3/T13, EU Art 10 | threat-model C1 (ISOLATION-FIX-PLAN.md) |

Two themes run through the gaps:

1. **Model-quality assurance** (eval, drift, bias, accuracy metrics, model cards)
   — Brightworks governs *how an agent is allowed to act*, not *how good the
   model's outputs are over time*. That is the E2 workstream.
2. **Detection depth & identity** — heuristic guardrails and regex PII (E3), the
   unauthenticated approval bearer link (M5), and pool-mode isolation being
   advisory rather than structural (C1) are the known residual risks. The
   strongest coverage is where Brightworks is structural and unit-tested:
   traceability (audit ledger), human oversight (gates + operating mode + RBAC),
   risk classification, and the use-case inventory.

## What scores well

- **Traceability / logging** (EU Art 12, OWASP T8 Repudiation, NIST Manage
  documentation) — the append-only hash-chained audit ledger is a `full`.
- **Human oversight** (EU Art 14) — human gates + operating mode + approver RBAC
  compose to `full`.
- **Risk classification & proportional response** (NIST Map/Manage) — the
  data×access×autonomy model is `full`.
- **AI-system inventory** (NIST Govern, EU Art 11 in part) — the derived
  use-case register is `full`.
