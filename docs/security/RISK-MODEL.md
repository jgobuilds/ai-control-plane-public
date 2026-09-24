# Risk classification & proportional controls

Every routed request is scored for risk, and the controls applied scale with the
score. The model follows the industry consensus (NIST AI RMF, Databricks DASF,
Microsoft/Stanford agentic-risk guidance): **an agent's risk is a function of the
data it touches, the access/blast-radius it has, and how autonomously it acts.**
Reading sensitive data is not the same risk as acting on it unattended.

## The three dimensions (scored 1–3)

| Dimension | Source | 1 | 2 | 3 |
|---|---|---|---|---|
| **data** | the scope's controls | `pii:off` | `pii:warn` | `pii:block` / `silo` |
| **access** (blast radius) | request `action` | `advise`/`read` | `write`/`ingest` | `apply`/`send`/`execute` |
| **autonomy** | request `trigger` | `turn` (human-initiated) | `scheduled` / `event` | `proactive` (self-triggered) |

**Level** = the worst single dimension, escalated to `critical` when two or more
dimensions are maxed. So a confidential-but-read-only question is `high`, while
applying a change unattended is `critical`.

## Controls scale with the dimensions

Controls switch on per-dimension (thresholds in `policy.json` → `risk`), not off a
single collapsed number — so we don't gate a harmless read just because the data
is sensitive:

| Control | Turns on when | Effect |
|---|---|---|
| **requireVerify** | access ≥ 2 **or** autonomy ≥ 2 | forces a fresh-context verifier pass |
| **requireApproval** | access ≥ 3 **or** autonomy ≥ 3 | router returns `blocked:"approval-required"` until re-called with `approved:true` |
| **requireIsolation** | data ≥ 3 | confidential data must run in an `isolation:"silo"` scope (warn until phase-5 cutover, then block) |
| **blockCrossVendor** | data ≥ 3 | the verifier *and the executor* stay on the **same** vendor — confidential work is never sent to a second provider (addresses threat-model M4). A request whose tier lives on the second vendor is **escalated to the cheapest permitted tier**, not refused; the decision and the audit record carry `vendorEscalated`. Escalation is upward only, so it never answers a hard request with a weaker model. |

## How a caller declares its risk

n8n sends two fields (data comes from the scope automatically):

```jsonc
{ "prompt": "...", "scope": "tenant-a",
  "action": "apply",        // advise | read | write | ingest | apply | send | execute
  "trigger": "proactive",   // turn | scheduled | event | proactive
  "approved": false }        // set true only after the approval gate has run
```

**The four triggers.** `turn`: a person started this request. `scheduled`: a
timer started it. `event`: an external system started it (a monitoring webhook,
an incoming ticket); it is unattended like a schedule but not self-initiated, so
it scores the same (2) and forces a verify, not an approval. `proactive`: the
**agent** decided to act on its own, which is the one that needs a human first.
Omitted means `turn`.

> **`action` and `trigger` are caller claims, so the router binds them
> (threat-model H5).** With `risk.enforce.actionBinding: "block"`, an omitted or
> unknown `action` is refused (400), and so is a `trigger` the policy does not
> define. The declared verb then decides the tools the runner gets: a request
> declared `advise` receives only read-class tools, so the declaration is a limit,
> not a label. `scripts/workflow_lint.py` fails any lane whose router call omits
> `action`, and any schedule- or webhook-started lane that omits `trigger`.

The router computes risk, enforces the controls, and returns the full breakdown
in `decision.risk` — an **audit record** of why each control fired. On an
approval-required request it returns `blocked:"approval-required"`; the workflow
routes through the approval-gate sub-workflow and re-calls with `approved:true`.

## Behaviour (validated)

| Request | Level | Controls fired |
|---|---|---|
| tenant advisory read | **high** | isolation, no-cross-vendor |
| internal code-edit (turn) | medium | verify |
| incident apply (proactive) | **critical** | verify, approval |
| incident-responder as it declares itself (`write`, `event`) | medium | verify |
| tenant apply (proactive) | **critical** | verify, approval, isolation, no-cross-vendor |
| scheduled dependency write (block-PII scope) | **high** | verify, isolation, no-cross-vendor |

Note how this *reproduces* the gates we built by hand (incident-apply → approval;
dependency-bumps → verify but no pre-gate because the gate is the merge).
`incident-responder` declares `event`, not `proactive`: an alert from monitoring
starts it, the agent does not decide to act. So its steps are verified rather
than pre-approved, and the human approval for "Apply fix" comes from the
approval-gate sub-workflow the lane already runs before that step. Declaring
`proactive` would have put Diagnose, a read plus a report, behind an approval
the lane has no step to satisfy. The
risk layer makes that logic explicit, uniform, and auditable instead of
per-workflow tribal knowledge.

## Operating mode — who leads, who validates (orthogonal to risk)

Risk sets *how hard* to gate. **Mode** sets *the human–AI relationship* — a
separate axis. The four-quadrant framing is **Sol Rashidi's Executive AI
Compass™ (© 2025 Sol Rashidi)**; we map its quadrants onto enforceable controls.
The point of the framework: pick the direction deliberately per function, and
remember that "support" is substantive (validating false positives/negatives,
final judgment), not a glance.

| Mode | Who leads | Agent may (max `action`) | Human's role | We enforce |
|---|---|---|---|---|
| `ai-only` | AI | up to `execute` | none | risk controls only |
| `ai-led` | AI, human-supported | up to `execute` | **validates the output** before it's authoritative | `requiresOutputValidation` stamp → workflow routes the result through the approval gate |
| `human-led` | human, AI-supported | up to `write` (drafts/edits) | **is the actor** (merges, sends) | agent capped below `apply`/`send`; output `advisory` |
| `human-only` | human | up to `advise` | does everything | agent capped at advisory assistance |

- **Set `mode` per scope** in `scopes.json` (a deliberate per-function choice).
  A request may only make it **more** restrictive, never less — a `human-only`
  scope can't be escalated to `ai-only` by a caller.
- The router **caps the agent's action** to the mode's `maxAction` (a request
  exceeding it returns `blocked:"mode-forbids-action"`), and stamps
  `decision.mode` / `advisory` / `requiresOutputValidation` so the workflow knows
  whether the human is the actor or the validator.
- **Mode and risk compose** — effective controls are the union. `ai-led` forces
  output validation even on a low-risk task; a `critical`-risk task adds
  pre-dispatch approval on top. Neither can weaken the other.

Rashidi's oncology example, in these terms: scan detection is `ai-led` — the
model drives detection (`maxAction: execute`), and a clinician **validates the
output** (`humanValidation: output`). Flipping it to `human-led` would cap the AI
at advisory and forfeit the speed that made it worth using — which is exactly the
mis-architecture the framework warns against. Here that direction is one config
field, chosen per scope.

## Scope & limits

- **Enforced on the `/route` path.** Lanes that still call runners directly
  (`fanout`/incident/dependency — threat-model C2) get risk enforcement once the
  C1/C2 runner cutover lands (the in-runner PEP). Until then they keep their
  own gates.
- **`data` is only as good as the PII/scope config** (threat-model M2) — a scope
  mislabeled `pii:off` scores low. Classification is downstream of honest scope
  controls.
- Tune everything in `policy.json → risk`; no code changes.
