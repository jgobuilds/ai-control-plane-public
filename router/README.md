# Cost-tiering router

The single entry point for agentic work. n8n calls **`POST /route`** and the
router picks the cheapest capable path, enforcing the policy so cost decisions
can't be bypassed. This is the "manage costs with a tiering" layer.

## The tiering (three rules, in order)

1. **Deterministic first.** Every request hits `rules.js` (Tier 0) before any
   model. If a regex/lookup/template can answer it, it does — **zero LLM cost.**
   Add your high-frequency, well-defined cases there.
2. **Cheapest capable model.** If a model is needed, the task's `task_type` maps
   to a tier in `policy.json`; the router always picks the **lowest** tier that
   fits. Small/cheap (Gemini Flash-Lite) for classify/extract/transform; standard
   (Sonnet) for generate/edit; frontier only where earned.
3. **Frontier where it counts.** `plan`, `architect`, `test-design`, and hard
   `review` route to the frontier tier (Fable). A hard **`maxTier` ceiling** in
   policy stops automation lanes from spending frontier tokens unless a request
   explicitly passes `allowFrontier:true`.

## Request / response

```jsonc
// POST /route  (header: x-router-token)
{
  "prompt": "Design the schema for a multi-tenant billing service",
  "task_type": "architect",     // optional; omit and the router triages cheaply
  "goal": "correct, normalized, handles tenant isolation",  // for verify
  "verify": true,               // optional; cross-checks on the other provider
  "provider": "claude",         // optional override (if policy allows)
  "model": "claude-opus-4-8",   // optional override
  "allowFrontier": true,        // optional; lifts the maxTier ceiling for this call
  "maxTurns": 40
}
```
```jsonc
// response
{
  "decision": { "tier": "t3", "provider": "claude", "model": "claude-fable-5",
                "task_type": "architect", "viaTriage": false, "clamped": false },
  "result":   { "result": "…the work…" },
  "verdict":  { "pass": true, "reasons": ["…"], "verifiedBy": "gemini" }
}
```

The `decision` block is your **audit trail** — every response says which tier and
model were used and why, so cost is observable (`scripts/gen_status.py` renders it).

## Tuning it — edit config, not code

- **`policy.json`** — tier→model mapping, task_type→tier routing, the `maxTier`
  ceiling, and whether requests may override. This is policy-as-code; change
  routing here.
- **`rules.js`** — deterministic Tier-0 handlers. The more you encode here, the
  less you spend on models. Prefer this whenever a task is well-defined.

## How verify picks a different model

Cross-verification runs the reviewer on the **other** provider (Claude executes →
Gemini reviews, or vice-versa) at that provider's model tier. Two vendors ≈
maximally independent fresh context — stronger than same-model self-review, and
it splits cost across both free/subscription lanes.
