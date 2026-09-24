// The dispatch lane end to end, without running an agent: the payload
// scripts/dispatch.py builds must pass the router's own gates under the SHIPPED
// policy, and the workflow must turn a router refusal into a message.
//
// Found 2026-09-18: dispatch had never routed a ticket. Its payload had no
// prompt, so the router refused it (400), and nothing read the router's answer.
//
//   node --test tests/unit/dispatch-lane.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const p = (rel) => fileURLToPath(new URL(rel, import.meta.url));
const policy = JSON.parse(readFileSync(p("../../router/policy.json"), "utf8"));
const actions = (await import(p("../../router/actions.js"))).default;
const wf = JSON.parse(readFileSync(p("../../n8n-workflows/dispatch.workflow.json"), "utf8"));

// The body dispatch.py produces for a default "implement" ticket. Kept literal
// here, and tests/dispatch_test.py asserts dispatch.py produces these values,
// so the two cannot drift silently.
const PAYLOAD = { prompt: "brief", goal: "g", task_type: "code-edit", action: "write", trigger: "scheduled" };
const scope = { name: "internal", controls: { mode: "human-led", pii: "warn", isolation: "pool" } };

test("dispatch payload passes the router's binding under the shipped policy", () => {
  assert.equal(policy.risk.enforce.actionBinding, "block", "this test is only meaningful under block");
  const b = actions.bindAction(PAYLOAD, scope, policy);
  assert.equal(b.bound, true);
  assert.equal(b.effective, "write");
  assert.ok(b.narrowing.tools.includes("Edit"), "implementation work gets write tools");
  assert.ok(!b.narrowing.tools.includes("Bash"), "but not execute-class ones");
});

test("the OLD dispatch payload is refused under block (the regression this fixes)", () => {
  assert.throws(() => actions.bindAction({ task_type: "implement", action: "advise", trigger: "dispatch" }, scope, policy),
    (e) => e.statusCode === 400 && /trigger/.test(e.message));
});

test("write fits a human-led scope's mode cap, so the lane is not refused by mode", () => {
  const cap = policy.modes.map["human-led"].maxAction;
  assert.ok(policy.risk.access.write <= policy.risk.access[cap], `write <= ${cap}`);
});

// ---------- the refusal message ----------
const code = wf.nodes.find((n) => n.name === "Format router refusal").parameters.jsCode;
function run(json) {
  const claim = { dispatched: 7, owner: "dex" };
  const fn = new Function("$input", "$", `return (() => { ${code} })();`);
  return fn({ first: () => ({ json }) }, () => ({ first: () => ({ json: claim }) }))[0].json;
}

test("a 400 names the ticket and the router's reason", () => {
  const out = run({ statusCode: 400, body: { error: "prompt is required" } });
  assert.match(out.title, /#7/);
  assert.match(out.message, /HTTP 400/);
  assert.match(out.message, /prompt is required/);
});

test("a 200 that stopped at a control says which control", () => {
  const out = run({ statusCode: 200, body: { blocked: "approval-required", message: "risk=high" } });
  assert.match(out.message, /approval-required/);
  assert.match(out.message, /needs a person/);
});

test("the workflow only raises it when the router did not take the work", () => {
  const cond = wf.nodes.find((n) => n.name === "Did the router take it?").parameters.conditions.conditions[0].leftValue;
  const expr = cond.replace(/^=\{\{\s*/, "").replace(/\s*\}\}$/, "");
  const decide = (json) => new Function("$json", `return (${expr});`)(json);
  assert.equal(decide({ statusCode: 200, body: { result: {} } }), false, "accepted work is not a refusal");
  assert.equal(decide({ statusCode: 400, body: { error: "x" } }), true);
  assert.equal(decide({ statusCode: 200, body: { blocked: "verify-unavailable" } }), true);
});
