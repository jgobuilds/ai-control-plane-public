# Bind what a request may do instead of trusting the caller's verb — and let the business name its operations

**Date:** 2026-09-17 · **Lens:** `ai-standards/references/proving-controls.md`,
`change-safety.md`, `cost-awareness.md`

## Considered

Facts came from probes of this system, not from documentation — the documentation
was one of the things found to be wrong:

- `router/server.js` scored access from `input.action ?? "advise"`, and the mode cap
  read the same caller-supplied field.
- `/fanout` ran no risk classification and no mode cap, and forwarded per-task
  `allowedTools` verbatim.
- Triage and the verifier were dispatched with no tool list.
- **Live CLI probe, 2026-09-17, claude 2.1.220, inside a runner as its own user:**
  `--dangerously-skip-permissions --allowedTools Read` → Bash **ran**;
  `--tools Read` → Bash unavailable. A second probe: `--tools` does not govern MCP
  tools; `--disallowedTools mcp__<server>` removes a whole server.
- `n8n-workflows/`: five `/route` call sites. `research-watch` declares `advise`
  (correct — its prompt only asks for advice); both `incident-responder` calls omit
  the verb; `dispatch` forwards its caller's payload.
- The second-vendor runner, as changed alongside this, refuses **every** tool list —
  including `[]` — with a 422 until its read-only mode (`READONLY_ARGS`) is
  configured, which ships unset and unprobed.
- The live scope tree declares no `allowedTools` on any scope, and every scope is
  `human-led`.

| Option | Cost | Verdict |
|---|---|---|
| **A. Score from a capability floor** — derive the minimum verb from the scope's `allowedTools`; the declared verb may raise it, never lower it | $0 licence. Non-dollar: **wrong premise** — the floor is computed from a list the probe shows restricts nothing, so the true floor is `execute` on every runner. Applied to the live tree it makes every request execute-class: approval on everything, and `mode-forbids-action` on everything that approval cannot clear | **Reject** — first plan, invalidated by the probe before it shipped |
| **B. Bind the verb: the effective verb narrows the tool set; named operations always bind; bare verbs `warn` by default** | $0 licence. Non-dollar: a runner contract change (`--tools`, `--disallowedTools`, `[]` means none); a `risk.toolAccess` table to maintain as the CLI adds tools, failing closed for anything unlisted; MCP governable per server only; lanes must name verbs before `block`; +1 approval per irreversible operation, by design | **Adopt** |
| **C. Keep trusting the caller, lint the workflows** for a declared verb | $0. Non-dollar: sees only n8n, and ADR 0018 plans runner-originated `/route` calls; a lint confirms a verb was *typed*, not that it is true | **Reject** — the rule this repo already holds for PII policy applies: a caller-declared control hands the bypass back to the caller it guards against |
| **D. Engine-level operations in `policy.json`** | $0. Non-dollar: puts business-specific operations in the neutral, published engine | **Reject** — operations belong to the scope tree, which for real work is the private overlay |
| **E. Adopt an external operation-model specification** | unpriced — no licensed, versioned spec suitable to depend on was found; pricing it needs one to exist | **Defer** — revisit if one ships; it would be read as data, with the router still deciding |

## Chose

1. **Operations, per scope:** `controls.operations.<name> = { access, irreversible?, tools? }`.
   A request naming one is scored by the operation, may raise but never lower it,
   and is refused (403) if the scope does not declare it.
2. **Irreversible means a human, at a level where segregation of duties applies** —
   risk is lifted to at least `high`, so the requester cannot approve their own act.
3. **One set of gates for both entrances.** `/route` and `/fanout` share
   `actionGates()`; a gate on one door and not the other is a choice of door.
4. **The router chooses every tool list it sends.** Bound requests get the narrowed
   set (and a refusal if none can be derived); triage and the verifier get `[]` where
   the runner can enforce it (item 5);
   fan-out tasks get the intersection of what they asked for and what was decided.
5. **Bound work stays on a runner that can enforce it** — the primary vendor, until
   `risk.enforce.secondVendorReadOnly` is the literal `true`; after that, only bound
   work above read-class. Same escalation path as the data rule, computed once for
   `escalateForVendor` and `targetFor`. Internal calls get `[]` only where it can be
   enforced, and today's behaviour elsewhere.
6. **Bare verbs ship `warn`.** Scoring is unchanged; the ledger records
   `declaredAction` (null when omitted), the governing verb, and what binding would
   drop. Only the literal `"warn"` relaxes the switch.

## Because

**Declaring is not doing.** The verb was the one input to the blast-radius score
that the caller controlled completely, and the control plane already knows better
than the caller in every other dimension: data sensitivity comes from the scope,
isolation from the tree, PII policy from the runner's environment. Access was the
exception, and it was the dimension that switches on human approval.

**Binding beats scoring.** Option A would have made the score *true* by inflating
every request to what the agent could do. B makes the declaration true by removing
what it did not declare. A cost approval on every live request; B costs nothing for
requests that are honest about themselves, which is the incentive the design wants.

**Business risk is not a verb.** "Send" covers a status email and an invoice. The
distinction the business actually needs — does money move, can it be undone, who may
approve — cannot be recovered from a verb, so it has to be declared by the people who
know, once, where the control plane can read it.

**What this gives up.** MCP granularity: an operation that needs a billing server
gets all of it, so its class is the server's class, and the remedy is narrower
servers rather than finer declarations. Speed on irreversible work: one human
approval each, by construction. And a maintenance surface: `risk.toolAccess` must
classify new CLI tools, and unlisted ones fail closed to `execute`, which will
occasionally narrow a request that did not need narrowing.

**What review caught that the tests did not.** The first version sent `[]` to
triage and the verifier unconditionally. Against the second-vendor runner that is a
422, and triage runs there for untyped requests on pooled scopes — so `warn`, the
mode promised to change nothing, would have broken untyped routing on merge. Every
unit test passed, because the fake runner accepted any payload. It was found by the
other half of this change reading the join between the two, not by a test. The fake
now honours each runner's contract, and re-introducing the defect fails four tests
with the runner's real refusal. The lesson is narrow and worth keeping: a fake that
accepts everything proves the caller's side of a contract and nothing about the
contract.

**What this does not close.** Internal calls to the second vendor keep an
unrestricted session until its read-only mode is probed and enabled. `trigger` (autonomy) is still caller-declared and has
the same shape. Tool narrowing is only as real as the runner's enforcement, which is
a separate change. And a declaration can be internally consistent and still wrong
about the business — CI cannot know that a reporting operation should not hold the
invoicing server.

## Status

**Implemented on a branch, not merged.** Verified, and how:

- `tests/unit/actions.test.mjs`, 32 tests. Route-level tests written first and
  watched failing against the pre-binding router (16 red: "invoice the customer"
  scored access 1; an undeclared operation accepted). End-to-end tests intercept the
  router's HTTP dispatch and assert the tool lists that reach a runner, against a fake
  that honours each runner's contract.
- Ten deliberate mutations of the gates each turned the suite red, including the
  defect review found; files restored byte-identical.
- All existing Node suites pass, including against the runner branch's `policy.json`.
- `conformance_test.py` fails each of six malformed declarations on exactly its rule,
  and passes a valid one; the sample tree declares two operations so this runs in CI.

**Depends on** the runner-side change (enforced `--tools` / `--disallowedTools`, and
`risk.toolAccess` + `risk.enforce.actionBinding` in `policy.json`). Without it this
branch's conformance fails on the missing key — deliberately, since a missing key
means `block`.

**Revisit when:** the second-vendor runner's read-only mode is probed against a live
authenticated session (then enable `secondVendorReadOnly`, recovering cheap-tier
routing for bound read-only work and closing the internal-call residual); the ledger shows no omitted `declaredAction` from any lane over a
representative period (the precondition for `block`); the CLI gains per-tool MCP
restriction (operations can then narrow within a server); or the first real business
operation is declared in the overlay, which is when the table in
[OPERATIONS.md](../design/OPERATIONS.md) meets a real system.
