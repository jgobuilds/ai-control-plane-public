# Approver RBAC + segregation of duties (threat-model M5)

**Problem (M5).** Today an approval is "anyone who clicks the n8n resume URL."
That URL is an unauthenticated bearer capability: it carries no approver
identity, enforces no role, and nothing stops the *requester* from approving
their own high-risk action. Human gates (GOVERNANCE #7) gate *that a human
clicked* — not *who*, and not *whether they were allowed to*.

This adds the missing authorization layer: **who may approve, at what risk, for
which scope, and never their own critical action.**

## What the router enforces

When a request reaches `/route` with `approved:true`, the router calls
`rbac.authorizedToApprove(...)` (in `router/rbac.js`) before it acts. The
approval is accepted only if **both** hold:

1. **Role (group) authorization.** The approver's `identityGroup`(s) must be in
   the set authorized to approve at this request's **risk level** (`policy.json →
   approvers.byRisk`). If the request's **scope** is explicitly listed under
   `approvers.byScope`, the approver must *also* be in one of that scope's
   groups — an additional restriction, never a relaxation.
2. **Segregation of duties (SoD).** For the risk levels in
   `approvers.segregationOfDuties.riskLevels` (default `high`, `critical`), the
   approver must be a **different identity** than the `requester`. SoD is checked
   *before* group membership, so being in an authorized group cannot buy you the
   right to approve your own critical action.

On failure the router returns `blocked:"approval-unauthorized"` with a reason,
and the decision is **still audited** — `requester`, `approver`, and
`approvalAuthorized:false` are written to the hash-chained ledger
(`audit/decisions.jsonl`), metadata only, no prompt/PII.

`authorizedToApprove` is **fail-closed**: a missing `approvers` policy, a missing
approver identity, or a risk level with no mapped group all return `{ok:false}`.

### Request shape

```jsonc
{ "prompt": "...", "scope": "client-a",
  "action": "apply", "trigger": "proactive",
  "approved": true,
  "requester":      "alice@example.com",          // who asked for the action
  "approver":       "carol@example.com",          // verified approver identity
  "approverGroups": ["security-approvers@example.com"] }  // approver's Google Groups
```

### Policy (`router/policy.json → approvers`)

```jsonc
"approvers": {
  "byRisk": {                         // every risk level MUST map to >=1 group
    "low":      ["agents-all@example.com"],
    "medium":   ["agents-approvers@example.com"],
    "high":     ["agents-approvers@example.com", "security-approvers@example.com"],
    "critical": ["security-approvers@example.com"]
  },
  "byScope": {                        // optional per-scope narrowing (on top of byRisk)
    "client-a": ["agents-consulting@example.com", "security-approvers@example.com"],
    "client-b": ["agents-consulting@example.com", "security-approvers@example.com"]
  },
  "segregationOfDuties": { "riskLevels": ["high", "critical"] }
}
```

Groups reference the same `identityGroup` namespace as `context/scopes.json`
(Google Groups). Tune entirely in policy — no code change.

## Identity capture — the design for n8n (the remaining half of M5)

Router-side RBAC is only as trustworthy as the `approver`/`approverGroups` it
receives. The resume URL in `n8n-workflows/approval-gate.subworkflow.json` is
unauthenticated, so **identity must be captured at the approval ingress**, not
inferred from the click. Two workable approaches:

**A. Authenticated approval endpoint (recommended).** Put the resume webhook
behind Google SSO / IAP (per `GSUITE-GCP.md`). IAP authenticates the approver and
injects signed headers:

- `X-Goog-Authenticated-User-Email` → the verified approver → `approver`
- resolve that user's Google Group membership (Directory API) → `approverGroups`

The workflow reads those on the resumed webhook and passes `approver` +
`approverGroups` (plus the original `requester`) to the router on the
`approved:true` re-call. IAP's headers are trustworthy because IAP strips any
client-supplied copies.

**B. Signed, expiring, per-approver links.** Instead of one shared resume URL,
issue a link bound to a **named** approver: a short-TTL token signed by the
router/n8n encoding `{approver, requester, requestId}`. Clicking it proves *that
approver* acted; the router verifies the signature and expiry, then applies the
RBAC/SoD checks. This resists link forwarding and replay without full SSO.

Either way the `requester` is known from the originating request, so SoD
(`approver !== requester`) is enforceable end-to-end.

Until one of these is in place, the resume link remains a bearer capability and
the RBAC guarantee is contingent — this is noted in the approval-gate `meta`
(`m5_identity_capture`) and tracked in `THREAT-MODEL.md` (M5) and
`GOVERNANCE.md`'s roadmap.

## Tests

- `tests/rbac_policy_test.py` — runnable now (no Node). Asserts the `approvers`
  policy is well-formed: every risk level maps to ≥1 group; byRisk/SoD keys are
  real levels; byScope entries are non-empty group lists.
- `tests/unit/rbac.test.mjs` — `node --test`. Covers SoD blocking
  approver==requester at critical, group mismatch blocked, the authorized happy
  path, scope narrowing, and fail-closed defaults.

## Limits

- **Enforced on the `/route` path only.** The direct runner lanes (threat-model
  C2) don't pass through this check until the C1/C2 in-runner PEP cutover.
- **Group membership is trusted input.** The router does not itself query the
  directory; it trusts the `approverGroups` presented. Approach A's IAP + server-
  side group lookup is what makes that trust sound — see above.
