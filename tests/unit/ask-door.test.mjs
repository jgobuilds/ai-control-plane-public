// The ask-human door must TELL its caller whether it was let in.
//
// It used to answer every POST with 200 "Workflow was started" (responseMode
// onReceived) BEFORE the auth node ran. Verified 2026-09-18 from a runner: the
// right token, the old RUNNER_TOKEN and no token at all all got 200; only n8n's
// log showed the rejections. So the seam heartbeat could not tell a locked door
// from an open one, and ask-human-mcp told an unauthorised agent its question
// had been raised.
//
// These run the workflow's REAL Code-node JavaScript, not a copy of it, against
// each kind of caller, and check the node graph answers before anything slow.
//
//   node --test tests/unit/ask-door.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const p = (rel) => fileURLToPath(new URL(rel, import.meta.url));
const door = JSON.parse(readFileSync(p("../../n8n-workflows/ask-webhook.workflow.json"), "utf8"));
const beat = JSON.parse(readFileSync(p("../../n8n-workflows/seam-heartbeat.workflow.json"), "utf8"));
const node = (wf, name) => {
  const n = wf.nodes.find((x) => x.name === name);
  assert.ok(n, `node ${name} exists`);
  return n;
};

// Run an n8n Code node's body with the globals it uses.
function runCode(code, { json = {}, env = {}, staticData = {} } = {}) {
  const fn = new Function("$input", "$env", "$getWorkflowStaticData",
    `return (() => { ${code} })();`);
  return fn({ first: () => ({ json }) }, env, () => staticData);
}

const TOKEN = "t".repeat(64);
const ENV = { ASK_HUMAN_TOKEN: TOKEN };
const req = (headers, body) => ({ headers, body });
const auth = () => node(door, "Authenticate + validate").parameters.jsCode;
const verdict = (json, env = ENV) => runCode(auth(), { json, env })[0].json;
const UUID = "123e4567-e89b-42d3-a456-426614174000";

// ---------- the auth node returns a verdict instead of throwing ----------
test("door: no token -> 401", () => {
  assert.equal(verdict(req({}, { healthcheck: true })).status, 401);
});
test("door: the runners' own token in the old header -> 401", () => {
  assert.equal(verdict(req({ "x-runner-token": TOKEN }, { healthcheck: true })).status, 401); // boundary-check: describes — a refused caller, not a call
});
test("door: a wrong value in the right header -> 401", () => {
  assert.equal(verdict(req({ "x-ask-human-token": "x".repeat(64) }, { healthcheck: true })).status, 401);
});
test("door: n8n itself has no ASK_HUMAN_TOKEN -> 503, never an open door", () => {
  const v = verdict(req({ "x-ask-human-token": "" }, { healthcheck: true }), {});
  assert.equal(v.status, 503);
});
test("door: authenticated healthcheck -> 200 with ok:true, asking nobody", () => {
  const v = verdict(req({ "x-ask-human-token": TOKEN }, { healthcheck: true }));
  assert.equal(v.status, 200);
  assert.equal(v.ok, true);
  assert.equal(v.healthcheck, true);
});
test("door: authenticated but no question -> 400", () => {
  assert.equal(verdict(req({ "x-ask-human-token": TOKEN }, { ticket: UUID })).status, 400);
});
test("door: authenticated but a non-UUID ticket -> 400", () => {
  assert.equal(verdict(req({ "x-ask-human-token": TOKEN }, { question: "q?", ticket: "../x" })).status, 400);
});
test("door: a valid question -> 202 carrying what the rest of the flow reads", () => {
  const v = verdict(req({ "x-ask-human-token": TOKEN }, { question: " Which folder? ", ticket: UUID, timeoutMinutes: 99 }));
  assert.equal(v.status, 202);
  assert.equal(v.ticket, UUID);
  assert.equal(v.question, "Which folder?");
  assert.ok(v.timeoutHours <= 10 / 60, "wait stays bounded at 10 minutes");
});

// ---------- the graph answers before anything slow, with the verdict's code ----------
const RESPOND = "n8n-nodes-base.respondToWebhook";
const next = (name) => ((door.connections[name] || {}).main || []).flat().map((c) => c.node);

test("door: the webhook waits for a Respond node instead of answering on receipt", () => {
  const wh = door.nodes.find((n) => n.type === "n8n-nodes-base.webhook");
  assert.equal(wh.parameters.responseMode, "responseNode");
});
test("door: every Respond node's status code comes from the verdict", () => {
  const rs = door.nodes.filter((n) => n.type === RESPOND);
  assert.ok(rs.length >= 1, "at least one Respond to Webhook node");
  for (const r of rs) {
    assert.match(String(r.parameters.options?.responseCode ?? ""), /\$json\.status/,
      `${r.name} must answer with the verdict's status, not a constant`);
  }
});
test("door: a caller is answered BEFORE a human is asked (the wait is 10 minutes)", () => {
  // Walk back from "Ask the human": a Respond node must sit on every path to it.
  const parents = (name) => Object.entries(door.connections)
    .filter(([, c]) => (c.main || []).flat().some((x) => x.node === name)).map(([s]) => s);
  const seen = new Set();
  const answeredOnAllPaths = (name) => {
    if (door.nodes.find((n) => n.name === name)?.type === RESPOND) return true;
    if (seen.has(name)) return true;
    seen.add(name);
    const ps = parents(name);
    return ps.length > 0 && ps.every(answeredOnAllPaths);
  };
  assert.ok(answeredOnAllPaths("Ask the human"));
});
test("door: a refused caller is answered AND still raises the alarm", () => {
  // The old throw paged via the Global Error Handler. Making the status visible
  // must not silently drop that security signal.
  const refused = door.nodes.find((n) => n.type === RESPOND && /refus/i.test(n.name));
  assert.ok(refused, "a Respond node for refusals");
  const after = next(refused.name).map((n) => node(door, n));
  assert.ok(after.some((n) => n.type === "n8n-nodes-base.code" && /throw new Error/.test(n.parameters.jsCode)),
    "a node after the refusal throws, so the error workflow still alerts");
});

// ---------- the heartbeat trusts a body, not just a 200 ----------
const decide = () => node(beat, "Decide (2 strikes)").parameters.jsCode;
test("heartbeat: 200 with ok:true is healthy", () => {
  const st = {};
  assert.deepEqual(runCode(decide(), { json: { statusCode: 200, body: { healthcheck: true, ok: true } }, staticData: st }), []);
  assert.equal(st.consecutiveFailures, 0);
});
test("heartbeat: a bare 200 (the old answer-on-receipt door) is NOT healthy", () => {
  const st = {};
  runCode(decide(), { json: { statusCode: 200, body: { message: "Workflow was started" } }, staticData: st });
  assert.equal(st.consecutiveFailures, 1);
});
test("heartbeat: 503 names the n8n-side misconfiguration", () => {
  const st = { consecutiveFailures: 1 };
  const out = runCode(decide(), { json: { statusCode: 503 }, staticData: st });
  assert.match(out[0].json.title, /503/);
  assert.match(out[0].json.message, /ASK_HUMAN_TOKEN/);
});
