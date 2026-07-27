# Governance — control catalog

What governs an agent request in Brightworks, and how each control is *enforced*
(not just documented). Ordered as a request flows through the router.

| # | Control | Where | Enforcement | Proof / audit |
|---|---|---|---|---|
| 0 | **Kill switch + circuit breaker** | `control/halt.json` + `policy.json limits` + `router/killswitch.js` | Evaluated at the top of `route()` before dispatch: an operator halt (global / scope / ancestor) or a rate-budget breaker stops the request — `blocked:"halted"\|"circuit-open"` | `decision.blocked`, `scripts/halt.py status`, `killswitch.test.mjs` |
| 1 | **Context scope** | `scopes.json` + mounts | An agent sees only its scope + ancestors; siblings aren't on disk (per-root runners, phase 1) | `conformance_test.py` mount-isolation checks |
| 2 | **Ethical walls** | `conflicts.pairs` | Conflicting scopes never share a runner | conformance test + generator assertion |
| 3 | **PII** | scrubber + router guard | Tokenized on ingest; `pii:block` refuses raw PII in prompts | `decision.pii` (types+counts, never values) |
| 3a | **Injection/output guardrails** | `policy.json guardrails` + `router/guardrails.js` | Heuristic `inputScan` warns/blocks on injection shapes; `outputScan` flags secret/exfil leakage. **Detection is pluggable** — optional Presidio/DLP (PII) + Lakera/Bedrock (injection) behind the heuristic default + fail-safe fallback (DETECTION-BACKENDS.md). | `decision.guardrails` (types only), `guardrails_test.py`, `detection_backend_test.py` |
| 3b | **Tool allowlisting** | `scopes.json controls.allowedTools` | Router passes per-scope tool allowlist to the runner (`--allowedTools`); unlisted tools denied | scope config + runner arg |
| 4 | **Risk** (data×access×autonomy) | `policy.json risk` | Proportional controls: verify / approval / isolation / no-cross-vendor | `decision.risk` |
| 5 | **Operating mode** (who leads) | `policy.json modes` | Caps agent action; stamps advisory vs output-validation | `decision.mode` |
| 6 | **Cost tier** | `policy.json tiers` | Cheapest-capable model; hard `maxTier` ceiling | `decision.tier` |
| 7 | **Human gates** | n8n approval-gate | Ship / promote / apply pass a human; timeout = deny | approval links + audit |
| 7a | **Approver RBAC + SoD** | `policy.json approvers` + `router/rbac.js` | An `approved:true` request must name an approver in an authorized `identityGroup` for the risk level/scope, and approver ≠ requester at high/critical (segregation of duties); else `blocked:"approval-unauthorized"` | `decision.requester/approver`, `rbac_policy_test.py`, `rbac.test.mjs` |
| 8 | **Audit ledger** | router `/audit` | Every decision hash-chained, append-only | `verify_audit.py` |
| 9 | **Retention & deletion** | `scopes.json retentionDays` + `scripts/retention_sweep.py` + `scripts/offboard_scope.py` | Effective `retentionDays` (node overrides levelDefault) ages out `ingest`/`deliverables` files; offboarding deletes a scope's subtree + vault and emits a signed deletion certificate | `audit/deletions/*.json` (pre-deletion sha256 manifest), `retention_test.py`, see RETENTION.md |
| 10 | **AI use-case register** | `scopes.json` (`owner`/`useCase`/`status`) + `scripts/gen_usecase_register.py` | Conformity inventory: every use case with its DERIVED default risk, operating mode, owner, and status — generated from the same artifacts the router enforces, so it can't drift | `docs/usecase-register.md` + `usecase-register.html`, `usecase_test.py`, see USECASE-REGISTER.md |
| 11 | **Framework-coverage map** | `compliance/framework-map.yaml` + `scripts/gen_compliance_map.py` | Maps every control above to OWASP Agentic / NIST AI RMF / EU AI Act requirements with honest full/partial/none coverage; not an enforcement gate — a conformance-evidence artifact for auditors | `docs/compliance-map.md`, `compliance_test.py`, see COMPLIANCE-MAP.md |
| 12 | **Eval / drift lane** (observability) | `scripts/eval_metrics.py` + `n8n-workflows/eval-drift.workflow.json` | Continuous eval **from the audit ledger** (verifies the hash chain first, refuses if broken): agentic-leverage proxy + verify-pass / change-failure / block / guardrail / PII / approval / risk-tier mixes, trended across sub-windows with drift WARNs. Not an enforcement gate — a measurement/observability artifact (metadata proxy, not semantic eval) | `eval_metrics_test.py`, weekly Notify, see EVAL.md |
| 13 | **Quality regression / improvement loop** (observability) | `scripts/eval_run.py` + `scripts/eval_capture.py` + `eval/datasets/**` + `n8n-workflows/feedback.workflow.json` | Offline **semantic/ground-truth** eval — the quality-regression counterpart to #12: prod failure → feedback flag → `eval_capture` (prompt tokenized via the scrubber) → `eval_run` replays each case through `/route` and grades it (exact/contains/regex/LLM-as-judge) → **exit non-zero on any regression** (CI quality gate). Governed observability: eval data is per-scope, PII-tokenized, retention-bound, local — never cross-scope, never a third-party processor. Not a runtime enforcement gate — a pre-ship quality gate + governed eval store | `eval_run_test.py`, `eval/datasets/starter/`, see IMPROVEMENT-LOOP.md |

## Audit ledger

The router appends one tamper-evident record per decision to
`audit/decisions.jsonl`. **Metadata only** — never the prompt (a SHA-256 + length
stands in) and never PII values (types + counts). Each line is `<hash> <json>`
where `hash = sha256(prevHash + json)`, so any edit, deletion, or reorder breaks
the chain.

```
python scripts/verify_audit.py     # re-walks the chain; non-zero on tamper
```

Captured per record: scope, mode (+advisory/output-validation), risk (level,
dims, controls), action/trigger, approved, blocked-reason, tier/provider/model,
PII types+count, verdict, prompt hash+length, chain hashes. This is the substrate
for compliance evidence, incident forensics, and the agentic-leverage metric.

> Limit: hash-chaining detects tampering; it doesn't *prevent* it. For
> non-repudiation, ship the ledger to WORM storage / append-only object storage
> (or periodically anchor the head hash externally). See GSUITE-GCP.md.

## Conformance tests

The governance rules are the product, so they get regression protection:

```
python scripts/conformance_test.py    # exits non-zero on any violation
```

Asserts, from the source-of-truth artifacts: ethical walls hold; every
confidential scope gets a dedicated runner; **no runner mounts a foreign scope**
(the structural C1 proof); mode caps are monotonic; risk controls never *weaken*
as a dimension rises; the PII guard is configured. Run it in CI on every change to
`scopes.json` / `policy.json` (regenerate `compose.scopes.yml` first).

The full unit + functional + security test suite — how to run each layer and what
invariant each test proves — is documented in **[TESTING.md](../runbooks/TESTING.md)**.

## On the roadmap (not yet built)

Authenticated approval ingress (the remaining half of M5 — router-side approver
RBAC + SoD is built; see RBAC.md).

*(Retention & right-to-deletion enforcement is now built — see control #9 above
and RETENTION.md.)*
