// Unit tests for approver RBAC + segregation of duties (threat-model M5).
// Runnable in CI:  node --test tests/unit/rbac.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import rbac from "../../router/rbac.js";
const { authorizedToApprove } = rbac;

const policy = {
  approvers: {
    byRisk: {
      low: ["agents-all@example.com"],
      medium: ["agents-approvers@example.com"],
      high: ["agents-approvers@example.com", "security-approvers@example.com"],
      critical: ["security-approvers@example.com"],
    },
    byScope: {
      "client-a": ["agents-consulting@example.com", "security-approvers@example.com"],
    },
    segregationOfDuties: { riskLevels: ["high", "critical"] },
  },
};

test("SoD: approver === requester is blocked at critical risk", () => {
  const r = authorizedToApprove(
    {
      requester: "alice@example.com",
      approver: "alice@example.com",
      approverGroups: ["security-approvers@example.com"], // in an authorized group…
      scope: null,
      riskLevel: "critical",
    },
    policy
  );
  assert.equal(r.ok, false);
  assert.match(r.reason, /segregation of duties/i);
});

test("group mismatch is blocked", () => {
  const r = authorizedToApprove(
    {
      requester: "bob@example.com",
      approver: "carol@example.com",
      approverGroups: ["agents-all@example.com"], // not authorized for critical
      scope: null,
      riskLevel: "critical",
    },
    policy
  );
  assert.equal(r.ok, false);
  assert.match(r.reason, /not in an authorized group/i);
});

test("authorized approver: happy path ok", () => {
  const r = authorizedToApprove(
    {
      requester: "bob@example.com",
      approver: "carol@example.com",
      approverGroups: ["security-approvers@example.com"],
      scope: null,
      riskLevel: "critical",
    },
    policy
  );
  assert.equal(r.ok, true);
});

test("scope narrowing: approver must ALSO be in the scope's group", () => {
  const r = authorizedToApprove(
    {
      requester: "bob@example.com",
      approver: "dave@example.com",
      approverGroups: ["agents-approvers@example.com"], // ok for byRisk high, NOT in client-a byScope
      scope: "client-a",
      riskLevel: "high",
    },
    policy
  );
  assert.equal(r.ok, false);
  assert.match(r.reason, /scope "client-a"/);
});

test("fail-closed: no approver identity supplied", () => {
  const r = authorizedToApprove(
    { approverGroups: ["security-approvers@example.com"], riskLevel: "critical" },
    policy
  );
  assert.equal(r.ok, false);
  assert.match(r.reason, /no approver identity/i);
});

test("fail-closed: no approvers policy", () => {
  const r = authorizedToApprove(
    { approver: "carol@example.com", approverGroups: ["security-approvers@example.com"], riskLevel: "critical" },
    {}
  );
  assert.equal(r.ok, false);
});

test("SoD does not block a distinct authorized approver", () => {
  const r = authorizedToApprove(
    {
      requester: "alice@example.com",
      approver: "carol@example.com",
      approverGroups: ["security-approvers@example.com"],
      scope: null,
      riskLevel: "high",
    },
    policy
  );
  assert.equal(r.ok, true);
});
