# AI use-case register — the conformity inventory

Brightworks knows, at any moment, **every AI use case it runs, who owns it, how
risky it is, and who leads the human/AI relationship** — an executive-facing,
EU-AI-Act-style conformity inventory. It is *derived*, not hand-kept: the register
is generated from the same source-of-truth artifacts the router enforces, so it
can never drift from what actually ships.

## Regenerate

```
python scripts/gen_usecase_register.py
```

Reads `context/scopes.json`, `router/policy.json`, and `n8n-workflows/*.json`;
writes two views of the **same** data:

| Output | For |
|---|---|
| `docs/usecase-register.md` | in-repo / PR review — a Markdown table + the Lanes table |
| `usecase-register.html` | executives — self-contained (inline CSS + vanilla JS), click any header to sort, readable light/dark, no external resources |

Regenerate whenever a scope, policy, or workflow changes. `tests/usecase_test.py`
asserts the generator runs and produces both files.

## What the columns mean

Everything except the register-specific fields (`owner`, `useCase`, `status`) is
**derived the way the router derives it**, so the inventory reflects enforcement
rather than a parallel spreadsheet.

| Column | Source / derivation |
|---|---|
| **Scope** | node key in `scopes.json` (its position in the scope tree) |
| **Level** | the scope's `level` (enterprise / department / team / personal) |
| **Use case** | node `useCase` — plain-English description of what the agent does |
| **Owner** | node `owner` — the accountable human |
| **Status** | node `status` — one of `proposed` \| `approved` \| `in-production` \| `retired` |
| **Risk (default)** | the risk **level** of a *baseline* request against the scope. The data dimension comes from the scope's effective PII/isolation (`silo`/`block` → 3, `warn` → 2, `off`/`none` → 1); the request `action`/`trigger` sit at their policy defaults. Dimensions combine as the router does — worst single dimension, escalated to `critical` when ≥ 2 dimensions are maxed. A **live** request (writing, applying, self-triggered) can only score **higher** — see RISK-MODEL.md. |
| **Mode** | the scope's operating mode — its `controls.mode`, else `modes.default`. Who leads / who validates (Executive AI Compass, below). |
| **Isolation** | effective `isolation` (`pool` \| `silo`); node `controls` override `levelDefaults[level]` |
| **PII** | effective outbound-prompt PII guard (`off` \| `warn` \| `block`), same override rule |
| **Identity group** | effective `identityGroup` (node override, else the level default) — who may invoke the scope |

Effective-control resolution mirrors `scripts/gen_compose.py` (`effective_controls`):
a node's `controls` override its `levelDefaults[level]`; nothing else is special-cased.

## The register fields

Three optional, additive fields on any `scopes.json` node drive the inventory. They
are **descriptive metadata** — the router doesn't gate on them, so adding them to a
node is safe and never changes routing:

```jsonc
"client-a": {
  "level": "team", "parent": "consulting",
  "controls": { "pii": "block", "isolation": "silo", "mode": "ai-led" },
  "runnerHost": "claude-runner-client-a",
  "owner":   "Priya Nair",                       // accountable human
  "useCase": "Client-A advisory & code-delivery agent",
  "status":  "in-production"                      // proposed|approved|in-production|retired
}
```

`tests/usecase_test.py` enforces the register's integrity: every `in-production`
scope must name an `owner` and resolve to a valid operating mode, and every
`status` must come from the allowed set.

## Operating mode — the Executive AI Compass

The **Mode** column places each use case on one of four human/AI operating
relationships — AI-only, AI-led (human validates the output), human-led (human is
the actor, AI assists), and human-only. This four-direction framing is **Sol
Rashidi's Executive AI Compass**; the point is to choose the direction
*deliberately per function* — and to remember that "support" means substantive
review, not a glance. Brightworks maps each direction onto an enforceable
`maxAction` cap and a human-validation stamp (see RISK-MODEL.md and
`router/policy.json` → `modes`). Mode is orthogonal to risk: risk sets *how hard*
to gate, mode sets *who leads*.
