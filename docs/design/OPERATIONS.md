# Operations — what an agent may DO, named by the business

An agent that invoices a customer, issues a refund or sends a contract is not
"writing" or "sending" in the abstract. It is performing a **business operation**,
and the risk of that operation is set by what it does to the business — whether
money moves, whether a customer sees it, whether it can be undone — not by the
verb the caller happened to type.

This document is how to declare those operations so the control plane governs
them. The implementation is [`router/actions.js`](../../router/actions.js); the
decision is [ADR 0019](../decisions/0019-bind-actions-not-trust-them.md).

## The gap this closes

`/route` used to score a request's blast radius from `input.action` — a verb the
**caller** supplies, defaulting to `advise`, the least risky one. The operating-mode
cap read the same field. So a request that named nothing, or said `advise` while
its agent held Bash, skipped `requireVerify` and `requireApproval` entirely.

Two further doors were open for the same reason:

- **`/fanout` ran no risk classification and no mode cap at all**, and passed each
  task's tool list through exactly as the caller wrote it.
- **Triage and the verifier were dispatched with no tool list**, i.e. the runner's
  full default set including Bash — while being fed untrusted text (the raw prompt,
  the executor's output).

And underneath all of it: `--allowedTools` **restricts nothing** under
`--dangerously-skip-permissions`. Probed live on 2026-09-17 (claude 2.1.220): with
`--allowedTools Read`, Bash still ran; with `--tools Read`, it did not. The
documented per-scope tool allowlist had never enforced anything.

## Declaring an operation

Operations live on the scope, in the scope tree (so in the private overlay for real
work — they are business-specific):

```json
"billing": {
  "level": "team", "parent": "finance",
  "controls": {
    "mode": "ai-led",
    "allowedTools": ["Read", "mcp__billing", "mcp__billing-reports"],
    "operations": {
      "invoice-customer":   { "access": "send",  "irreversible": true, "tools": ["Read", "mcp__billing"] },
      "void-draft-invoice": { "access": "write", "irreversible": true, "tools": ["Read", "mcp__billing"] },
      "report-receivables": { "access": "read",                         "tools": ["Read", "mcp__billing-reports"] }
    }
  }
}
```

A request names the operation instead of (or as well as) a verb:

```json
{ "scope": "billing", "operation": "invoice-customer", "prompt": "Invoice Acme for September" }
```

### Choosing each field

| Field | Rule |
|---|---|
| `access` | The **worst** thing the operation's tools can do, from `policy.json` `risk.access`. Not what you intend this call to do — what the agent *could* do with what it is handed. |
| `irreversible` | `true` if it moves money, reaches someone outside the business, or cannot be undone by the business alone. When in doubt, `true`: the cost is one approval. |
| `tools` | Bare built-ins (`Read`, `Edit`) or **whole** MCP servers (`mcp__billing`). Every one must also be in the scope's `allowedTools`. |

**Split reading from acting.** `report-receivables` and `invoice-customer` touch the
same system but are different operations with different classes. Do not declare one
`billing` operation that does both — it takes the class of the worst thing it can do,
and every harmless report then needs a human.

### MCP servers are all-or-nothing

There is no flag that keeps one tool of an MCP server and drops another:
`--disallowedTools mcp__<server>` removes a whole server, and `--tools` does not
govern MCP at all (probed 2026-09-17). So an operation that needs
`mcp__billing` gets **every** tool that server exposes — reading, creating,
sending, voiding. Its `access` must be the class of the whole server. If that is too
broad, the fix is a narrower server (e.g. a read-only `mcp__billing-reports`), not a
narrower declaration.

## What the router does with it

| Situation | Result |
|---|---|
| Operation not declared on this scope | **403**, not a guessed default |
| Declaration malformed or understates its tools | **500**, and CI fails first (`conformance_test.py`) |
| Caller declares a verb below the operation | Scored as the operation; `underDeclared` recorded |
| Caller declares a verb above the operation | Honoured — a caller may ask for more scrutiny, never less |
| `irreversible: true` | `requireApproval`, and risk level at least `high`, so **the requester cannot approve their own invoice** (segregation of duties) |
| Operation class above the scope's mode cap | `mode-forbids-action`, even when approved — a `human-led` scope cannot have an agent invoice |
| Dispatch | The runner receives **only** the operation's tools; triage and the verifier receive none on the primary vendor, and today's tools on the second vendor until its read-only mode is enabled |
| Bound work | Kept on the primary vendor. The second-vendor runner can at most enforce read-only, and refuses every tool list until `risk.enforce.secondVendorReadOnly` is `true`; once it is, only bound work above read-class is kept off it |
| `/fanout` | The same gates, one decision for all tasks; a task may narrow its tools, never widen them |

Operations **always bind**. Nothing called `/route` with `operation` before this, so
there is no legacy traffic to protect.

## Bare verbs: `warn` until the ledger says otherwise

Requests that name a verb but no operation are governed by
`risk.enforce.actionBinding` in `policy.json`:

- **`warn`** (shipped): scoring is exactly as before. The ledger records
  `declaredAction` (null when omitted — never back-filled as `advise`), the verb
  governed by, and `actionBinding.wouldDrop` — the tools binding *would* remove.
- **`block`**: the effective verb narrows the tool set, and an omitted or unknown
  verb is refused (400). A declared `advise` then physically cannot write.

**Only the literal `"warn"` relaxes it.** A missing or misspelt value enforces, so the
key must stay present; `conformance_test.py` and `security_test.py` both fail without
it.

Before switching to `block`, the lanes that omit a verb must name one — at the time
of writing, both `incident-responder` calls omit it, and `dispatch` forwards whatever
its own caller sent. (`research-watch` declares `advise`, which is correct: its prompt
only asks for advice.) Read the ledger's `declaredAction` and
`actionBinding.wouldDrop` over a representative period first; switching without that
turns every omitting lane into a 400. Expect a cost change too: under `block` every
bound request stays on the primary vendor until `secondVendorReadOnly` is enabled.

## How this is proven

- `tests/unit/actions.test.mjs` — 32 tests. The route-level ones were written first
  and watched failing against the pre-binding router (it scored "invoice the
  customer" at access 1 and accepted an undeclared operation). The end-to-end ones
  intercept the router's own HTTP dispatch and assert what reaches a runner, against
  a fake that honours each runner's real contract — including that the second-vendor
  runner refuses every tool list with a 422 until its read-only mode is configured.
- Ten deliberate mutations — mode cap reading the caller's verb, irreversible not
  forcing approval, `/fanout` skipping the gates, triage getting default tools, the
  executor getting the scope's tools, a fan-out task widening, an undeclared
  operation being defaulted, `[]` sent to every internal call, bound read-only work
  allowed onto the second vendor by default, the enable key accepting any truthy
  value — each turned the suite red.
- **A defect found by review, not by tests.** The first version sent `[]` to triage
  and the verifier on every vendor. The second-vendor runner 422s any tool list while
  unconfigured, and triage runs there for untyped requests on pooled scopes — so
  `warn`, the mode meant to change nothing, would have broken untyped routing. The
  unit tests passed because the fake runner accepted everything; it now does not.
- `scripts/conformance_test.py` validates every declared operation in the active
  tree; the sample tree declares two so the check runs in CI rather than validating
  nothing there.

## Limits

- **Tool narrowing is only as real as the runner's enforcement.** The router decides
  the list; the runner turns it into `--tools` / `--disallowedTools`. The operation
  gates — class, approval, segregation of duties, mode cap — are enforced in the
  router regardless.
- **Internal calls to the second vendor keep today's tools** until
  `secondVendorReadOnly` is enabled, so a prompt injection reaching triage or the
  verifier *there* still lands in an unrestricted session. Probing that runner's
  read-only mode against a live, authenticated session is what closes it.
- **`trigger` is still caller-declared.** Autonomy (`turn` / `scheduled` /
  `proactive`) has the same shape as the verb did, and is not bound here.
- **A declaration can still be wrong.** CI catches malformed and self-contradicting
  ones; it cannot know that `report-receivables` should not have been given
  `mcp__billing`. That review is a human's.
