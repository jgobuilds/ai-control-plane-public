// Approver RBAC + segregation of duties (threat-model M5).
//
// Pure functions, no dependencies (mirrors the router: require only core). The
// router calls authorizedToApprove() whenever a request arrives with
// `approved:true`, so "approval" stops meaning "anyone who clicked the
// unauthenticated n8n resume URL" and starts meaning "a NAMED approver who
//   (a) belongs to an identityGroup authorized to approve at this request's risk
//       level (and scope, when the scope is explicitly listed), and
//   (b) is NOT the requester for high/critical risk — segregation of duties."
//
// Fail closed by construction: a missing approvers policy, missing approver
// identity, or an unmapped risk level all yield {ok:false}. The function never
// returns ok on ambiguity, and SoD is evaluated BEFORE group membership so it
// can't be satisfied away by belonging to an authorized group.
"use strict";

function asArray(v) {
  if (Array.isArray(v)) return v.filter((x) => typeof x === "string" && x);
  if (typeof v === "string" && v) return [v];
  return [];
}

function intersects(a, b) {
  const set = new Set(b);
  return a.some((x) => set.has(x));
}

// authorizedToApprove({requester, approver, approverGroups, scope, riskLevel}, policy)
//   -> { ok: boolean, reason: string }
function authorizedToApprove(
  { requester, approver, approverGroups, scope, riskLevel } = {},
  policy = {}
) {
  const cfg = policy && policy.approvers;
  if (!cfg || typeof cfg !== "object")
    return { ok: false, reason: "no approvers policy configured (fail-closed)" };

  if (!approver || typeof approver !== "string")
    return { ok: false, reason: "no approver identity supplied (unauthenticated approval refused)" };

  if (!riskLevel || typeof riskLevel !== "string")
    return { ok: false, reason: "no risk level to authorize the approval against (fail-closed)" };

  const riskGroups = asArray(cfg.byRisk && cfg.byRisk[riskLevel]);
  if (!riskGroups.length)
    return { ok: false, reason: `no approver groups mapped for risk level "${riskLevel}" (fail-closed)` };

  // (b) Segregation of duties FIRST: at the configured risk levels the approver
  // must be a different identity than the requester. Checked before group
  // membership so SoD can never be bypassed by being in an authorized group.
  const sodLevels = asArray(cfg.segregationOfDuties && cfg.segregationOfDuties.riskLevels);
  if (sodLevels.includes(riskLevel) && requester && approver === requester)
    return {
      ok: false,
      reason: `segregation of duties: approver "${approver}" is the requester; a different approver is required at risk="${riskLevel}"`,
    };

  // (a) Approver's group must be in the authorized set for this risk level.
  const groups = asArray(approverGroups);
  if (!intersects(groups, riskGroups))
    return {
      ok: false,
      reason: `approver "${approver}" (groups: ${groups.join(", ") || "none"}) is not in an authorized group for risk="${riskLevel}" (authorized: ${riskGroups.join(", ")})`,
    };

  // Optional per-scope narrowing: when the scope is explicitly listed, the
  // approver must ALSO belong to one of that scope's authorized groups (an
  // additional restriction on top of byRisk — never a relaxation).
  const scopeSpec = scope && cfg.byScope && cfg.byScope[scope];
  if (scopeSpec) {
    const scopeGroups = asArray(scopeSpec);
    if (!intersects(groups, scopeGroups))
      return {
        ok: false,
        reason: `approver "${approver}" is not in a group authorized to approve scope "${scope}" (authorized: ${scopeGroups.join(", ")})`,
      };
  }

  return {
    ok: true,
    reason: `approver "${approver}" authorized for risk="${riskLevel}"${scope ? ` scope="${scope}"` : ""}`,
  };
}

module.exports = { authorizedToApprove };
