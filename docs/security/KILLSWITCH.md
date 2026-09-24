# Kill switch + budget/rate circuit breaker

The ability to halt agent activity **instantly** is table-stakes governance. This
control gives an operator a manual stop and gives the system an automatic one,
both enforced at the very top of the router pipeline — before scope resolution,
before any model dispatch.

Two independent halts, evaluated first in `route()`:

1. **Kill switch** — a human operator flips `control/halt.json`. A global halt
   stops every request; a scope halt stops that scope **and every descendant**.
2. **Circuit breaker** — a rate/budget ceiling. The router counts recent
   decisions in the audit ledger and refuses new work once a threshold is met.

When either fires the router returns without dispatching, and the handler audits
the decision like any other — so a halt leaves a tamper-evident trail:

```
{ "decision": { "scope": "tenant-a" }, "blocked": "halted",      "reason": "…" }
{ "decision": { "scope": "jane" },     "blocked": "circuit-open", "reason": "…" }
```

## Kill switch — `control/halt.json`

Runtime state, **gitignored** (only `control/halt.example.json` is committed).
Shape:

```json
{ "global": false, "scopes": [], "reason": "", "ts": "" }
```

- `global: true` halts **all** requests.
- `scopes: ["consulting"]` halts `consulting` and every descendant. The router
  matches `halt.json` against the request's full scope **chain** (node + all
  ancestors), so halting a parent halts its children — halting `consulting`
  halts `tenant-a`.
- `reason` / `ts` are operator metadata for the audit trail; set by the CLI.

Enforcement lives in `router/killswitch.js`:

- `haltState(dir)` reads `control/halt.json`. Missing or corrupt ⇒ **not halted**
  (fail **safe** for availability — the kill switch is an operator escalation,
  not a security boundary), but a real read/parse error is logged.
- `isHalted(state, scopeChain)` ⇒ `{ halted, reason }` — true if `global` is on
  or any scope in the chain is listed.

### Operating it — `scripts/halt.py`

Runnable now (pure stdlib). Honors `CONTROL_DIR` (default `<repo>/control`).

```
python scripts/halt.py global "prod incident 1234"     # halt EVERYTHING
python scripts/halt.py scope tenant-a "data question"  # halt a scope + its descendants
python scripts/halt.py resume global                   # clear the global halt
python scripts/halt.py resume tenant-a                 # un-halt one scope
python scripts/halt.py resume                           # clear ALL halts
python scripts/halt.py status                           # show current state
```

## Circuit breaker — `policy.json` `limits`

```json
"limits": {
  "windowMinutes": 10,
  "maxRequestsPerScope": 60,
  "maxRequestsGlobal": 200
}
```

`breaker(policy, auditPath, scopeName)` in `router/killswitch.js` counts decisions
in `audit/decisions.jsonl` whose `ts` falls inside the sliding window and trips
(`blocked:"circuit-open"`) when the global count **or** the per-scope count
meets/exceeds its ceiling. It is deterministic and cheap (one line scan). Set a
ceiling to `null`/omit it to disable that dimension; set `windowMinutes <= 0` to
disable the breaker entirely.

Because the breaker reads the same append-only ledger every decision writes to,
no extra state is needed — the audit trail *is* the counter.

## Deployment

`docker-compose.yml` mounts `./control:/control` into the **router** service and
sets `CONTROL_DIR=/control`. An operator (or an alerting webhook) writes
`control/halt.json` on the host; the router picks it up on the next request. No
redeploy, no restart.

## Tests

- `tests/unit/killswitch.test.mjs` — `node --test`: `isHalted` (global / scope /
  ancestor / none) and `breaker` threshold logic (global + per-scope ceilings,
  window filtering).
- `tests/killswitch_test.py` — runnable now; drives `scripts/halt.py` against a
  temp control dir and asserts global + scope + ancestor halting.

## Limits

The kill switch is an **availability** control that fails *open* on a corrupt
flag file — deliberately, so a bad write can't wedge the router. It is not a
tamper-proof security boundary. Pair it with filesystem permissions on
`control/` and treat write access to `halt.json` as an operator privilege.
