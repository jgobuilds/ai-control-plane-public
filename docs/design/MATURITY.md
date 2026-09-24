# Maturity — where this stack actually is, and what moves each dimension up

**Self-assessed on 2026-09-24.** Nobody external has reviewed this. It is scored
against the scale below, which is this project's own, and every row names the
command or artifact you can run to check it rather than asking you to take the
rating on trust. Where a rating is generous, the row says so.

The honest summary: **enforced, thinly exercised.** Most controls are real code
with a test that has been watched failing. What is missing is *volume* — the
ledger holds ~100 decisions, and several lanes have never carried real work — so
almost nothing here has been proven under load, and one dimension (quality eval)
is barely started.

## The scale

| Level | Means |
|---|---|
| **1 — Ad hoc** | Nothing written, nothing enforced. Behaviour depends on who is at the keyboard. |
| **2 — Documented** | Written down and followed by habit. No mechanism fails when it is violated. |
| **3 — Enforced** | A check fails on violation, in CI or at runtime. Nobody has confirmed the check can fail. |
| **4 — Proven** | The check has been **watched failing** on purpose (mutation, probe, or a real incident), and the proof is re-run automatically. |
| **5 — Proven at scale** | Level 4 plus sustained evidence from real volume *and* review by someone outside the project. |

Level 5 is deliberately out of reach here: a single-operator deployment has
neither the volume nor the external audit, and claiming it would be the exact
failure this scale exists to prevent.

## Where each dimension sits

| Dimension | Level | Evidence you can check | Why not higher |
|---|---|---|---|
| **Policy enforcement at one chokepoint** | **4** | Only the router reaches a runner, enforced tree-wide by `scripts/boundary_check.py`; a request's tools are bound to its governing verb (`risk.enforce.actionBinding: "block"` since 2026-09-18); approver ≠ requester at high risk (`rbac_policy_test.py`). The CLI flag the runner depends on was probed live and **watched refusing** (`scripts/probe_cli_enforcement.py`). | Only ~3 bound decisions have ever flowed through in anger; the evidence for the binding is a static audit of every call site, not traffic. |
| **Auditability** | **4** | Every decision is appended to a hash-chained ledger; `scripts/verify_audit.py` checks the chain, and the eval lane refuses to read it if the chain is broken. Architecture, use-case register and compliance map are generated from the running system or its config, with CI failing on drift. | ~100 decisions total. A tamper test at 100 records says less than one at 100,000. |
| **Supply chain & change safety** | **4** | Images pinned `tag@digest` with a dated review gate; agent CLIs lockfile-pinned; 7-day release-age rule checked by `scripts/dependabot_check.py`; 30-day staleness ceiling (`pin_staleness.py`, weekly); Trivy daily on the images we *build*, not only those we pull; deploys go canary → observe → promote (`scripts/rollout.py`), which refuses to deploy from anywhere but the live checkout. | Each gate has been watched failing, but the rollout path has run few times, and `observe` has yet to judge a canary that real work reached. |
| **Isolation & data protection** | **3** | Per-scope mounts and ethical walls are conformance-tested; the PII vault is never mounted into a runner; PII is tokenized on ingest and raw PII in a prompt is refused; commit, commit-message and CI PII gates run on every repo. | **The retention sweep deletes nothing** — it is dry-run only ([#4](https://github.com/jgobuilds/ai-control-plane-public/issues/4)). Deletion is the half of data protection that cannot be faked, so this cannot reach 4 while it is a no-op. |
| **Reliability & diagnosability** | **3** | A recurrence register with 7 entries, each `harness-fixed` and each carrying a command the gate re-runs; 8 known-cause signatures matched before any model is asked; every lane bound to a global error handler. | The register is fed by occurrence counts that live host-side, so CI can only check the file's honesty. Behaviour under sustained failure is untested. |
| **Cost governance** | **3** | Deterministic rules answer well-defined tasks at zero model cost; each task type maps to a tier under a hard `maxTier` ceiling; every ADR must weigh cost in its options table (`scripts/adr_check.py`). | The ledger's cost figures are notional, and the volume is too small to show whether the tier mix is right. |
| **Compliance evidence** | **3** | A framework map generated from the same artifacts the router enforces, with deliberately honest `full` / `partial` / `none` coverage: OWASP Agentic 1 full / 12 partial / 1 none; NIST AI RMF 7 / 9 / 3; EU AI Act 2 / 8 / 1. Drift fails CI. | Self-mapped and self-rated. No assessor has looked at it, and most rows are honestly `partial`. |
| **Security posture** | **3** | A red-team threat model with 22 tracked findings: 11 closed, 5 partial, 5 open, 1 knowingly accepted — each closed finding naming a proof the gate runs (`tests/findings_assert_test.py`). | 5 open findings, and the isolation leg is the one the design leans on hardest. |
| **Quality eval** | **2** | The offline grader is tested in CI, and a metadata-proxy eval lane trends verify-pass, change-failure and drift weekly from the ledger. | **3 eval cases.** No semantic gate runs over real data, and run-to-run noise has never been measured. [ADR 0020](../decisions/0020-evolve-skills-wikis-graph-from-evidence.md) makes growing this the prerequisite for everything else. |
| **Autonomy (agentic ladder)** | **2** | AG-1 (assisted) and AG-2 (parallel, isolated worktrees, self-verifying, human merge) both work today. | AG-3 lanes exist but are **loaded and idle**; `dispatch` fires on schedule and has never claimed a real ticket ([#5](https://github.com/jgobuilds/ai-control-plane-public/issues/5)). Autonomy is scored by behaviour, not by code that could run. |

## The ramp

Ordered by what unblocks the most, not by what is easiest. Each step names the
observable that moves the dimension, because a ramp without one is a wish.

| # | Step | Moves | Done when |
|---|---|---|---|
| 1 | **Make deletion real.** Take the retention sweep out of dry-run behind a signed deletion certificate and a restore test. | Isolation 3 → 4 | A scope's expired files are gone, the certificate verifies, and the test has been watched failing. |
| 2 | **Grow the eval corpus** to ≥ 30 cases per scope from real captured failures, then measure run-to-run noise before gating. | Quality 2 → 3 | A paired comparison reaches a verdict (≥ 6 discordant pairs) instead of "inconclusive". |
| 3 | **Let one lane carry real work end to end** — `dispatch` claims a real ticket, the router binds the action, a human gates the exit. | Autonomy 2 → 3 | The ledger shows a real ticket dispatched, gated and shipped, and `rollout.py observe` has a canary with traffic to judge. |
| 4 | **Close the open security findings**, isolation first. | Security 3 → 4 | Each closed finding names a proof the gate runs. |
| 5 | **Earn volume, then re-score.** Everything at 4 stays there until real traffic tests it. | The 4s toward 5 | Enough decisions that a drift WARN means something, plus one outside reviewer. |

Steps 1–3 are independent; 4 runs alongside. Nothing here needs new
infrastructure, which is deliberate — see [ADR 0009](../decisions/0009-where-the-control-plane-runs.md).

## How to disprove this rating

Every number above comes from the repo, so it can be checked without trusting
the prose:

```bash
python scripts/conformance_test.py        # isolation, ethical walls, action binding
python scripts/verify_audit.py            # the ledger's hash chain
python scripts/boundary_check.py          # nothing but the router reaches a runner
python tests/findings_assert_test.py      # every "closed" finding's proof actually runs
python scripts/recurrence.py check        # the register is honest
python scripts/build_site.py --check      # what this site publishes
```

If a rating looks generous, the fastest disproof is usually the ledger: read
`audit/decisions.jsonl` and count how many decisions of the kind a control
governs have actually flowed through it.

## What is deliberately not published

This engine ships brand-neutral and AGPL. Three things stay private, and none of
them is a control:

- **Tenant and org content** — the real scope tree, org identity, prompts, client
  context and any vault data. It lives in a private overlay and is enforced out of
  this repo by a hygiene gate that runs on every commit *and* over the whole tree
  in CI.
- **Secrets** — never in the repo in any form. Tokens are per-service, held
  outside version control, and the ledger records types and counts, never values.
- **Commercial analysis and the internal gap-closure plan** — a strategy document
  about ourselves is not part of the engine. The *gaps themselves* are public, as
  issues, which is the half that matters to anyone evaluating this.

The mechanisms are public on purpose. A control that only works while its design
is secret is not a control.
