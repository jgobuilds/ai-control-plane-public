// Action binding — prove the router decides what a request may DO, instead of
// believing the caller's description of itself.
//
// WHY THIS FILE EXISTS. Risk and the operating-mode cap used to read
// `input.action`, a verb the caller supplies that defaults to "advise". A request
// that named nothing — most /route call sites in n8n-workflows/ — was scored as
// the least risky thing an agent can do while holding Bash. And a business
// operation such as invoicing a customer had no representation at all: it was
// whatever verb the lane author happened to type.
//
// The tests that matter most are the ROUTE-LEVEL ones against the real router
// module: they were written first and watched failing against the pre-binding
// code (classifyRisk ignored `operation`; an undeclared operation was accepted;
// an irreversible one needed no human). The pure-module tests below them pin the
// binding rules so a later edit cannot quietly loosen one.
//
// Runs as its own node:test process, with its own policy + scope fixtures, so the
// audit-chain assertions in router.test.mjs are unaffected.

import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import http from "node:http";
import { EventEmitter } from "node:events";

const p = (rel) => fileURLToPath(new URL(rel, import.meta.url));
const TMP = mkdtempSync(join(tmpdir(), "router-actions-"));

// ---------- fixtures ----------

// The agreed runner-side shape. A billing MCP server that can send and void is
// classed as `send` AS A WHOLE, because MCP is only enforceable per server.
const TOOL_ACCESS = {
  unlisted: "execute",
  tools: {
    Read: "read", Grep: "read", Glob: "read", LS: "read", NotebookRead: "read",
    TodoWrite: "advise",
    Edit: "write", MultiEdit: "write", Write: "write", NotebookEdit: "write",
    Bash: "execute", WebFetch: "send", WebSearch: "send",
    "mcp__ask-human": "advise", "mcp__n8n": "execute",
    "mcp__billing": "send",
    "mcp__billing-reports": "read",
  },
};

function policyWith(overrides = {}) {
  const base = JSON.parse(readFileSync(p("../../router/policy.json"), "utf8"));
  base.risk.toolAccess = TOOL_ACCESS;
  base.risk.enforce = { ...(base.risk.enforce || {}), actionBinding: "warn", ...overrides };
  return base;
}

const OPERATIONS = {
  "invoice-customer": { access: "send", irreversible: true, tools: ["Read", "mcp__billing"] },
  "void-draft-invoice": { access: "write", irreversible: true, tools: ["Read", "Edit"] },
  "report-receivables": { access: "read", tools: ["Read", "mcp__billing-reports"] },
  // Deliberately wrong: claims read but needs a server that can send.
  "lookup-invoice": { access: "read", tools: ["mcp__billing"] },
};

const scopes = JSON.parse(readFileSync(p("../../context/scopes.json"), "utf8"));
scopes.nodes["billing"] = {
  level: "team", parent: "engineering",
  controls: { pii: "warn", mode: "ai-led", operations: OPERATIONS },
};
scopes.nodes["billing-assist"] = {
  level: "team", parent: "engineering",
  controls: { pii: "warn", mode: "human-led", operations: { "invoice-customer": OPERATIONS["invoice-customer"] } },
};

const POLICY_PATH = join(TMP, "policy.json");
const SCOPES_PATH = join(TMP, "scopes.json");
writeFileSync(POLICY_PATH, JSON.stringify(policyWith()));
writeFileSync(SCOPES_PATH, JSON.stringify(scopes));

// ---------- a fake runner, in-process ----------
// The router reaches runners with http.request to `<name>-runner:8080`. Rather
// than add a redirect seam to the security-critical dispatch path, this process
// intercepts http.request itself and records exactly what the router SENDS —
// which is the fact under test: that the tool list reaching a runner is the one
// the router decided, for the executor, for triage, for the verifier and for
// every fan-out task. Anything that is not a runner call passes through.
const DISPATCHED = [];
const realRequest = http.request;
http.request = function fakeRunnerRequest(opts, cb) {
  if (!opts || typeof opts.host !== "string" || !opts.host.endsWith("-runner")) return realRequest.apply(this, arguments);
  const req = new EventEmitter();
  let body = "";
  req.setTimeout = () => req;
  req.destroy = () => req;
  req.write = (chunk) => { body += chunk; return true; };
  req.end = () => {
    const payload = JSON.parse(body || "{}");
    DISPATCHED.push({ host: opts.host, path: opts.path, payload });
    const prompt = String(payload.prompt || "");
    const result = prompt.startsWith("Classify this task") ? "generate"
      : prompt.includes("strict, independent reviewer") ? '{"pass": true, "reasons": []}'
      : "done";
    // FAITHFUL TO THE SECOND-VENDOR RUNNER'S CONTRACT: with no read-only mode
    // configured it refuses ANY tool list, the empty one included (422). An
    // accept-everything fake is exactly how a router change that sent `[]` there
    // passed every test here and was only caught in cross-review.
    const refuse = opts.host.startsWith("gemini") && Array.isArray(payload.allowedTools);
    const resp = new EventEmitter();
    resp.statusCode = refuse ? 422 : 200;
    queueMicrotask(() => {
      cb(resp);
      resp.emit("data", JSON.stringify(refuse
        ? { error: "gemini-runner has no READONLY_ARGS configured, so it cannot enforce a read-only tool list" }
        : { result }));
      resp.emit("end");
    });
    return req;
  };
  return req;
};
const dispatchedSince = (n) => DISPATCHED.slice(n);
const kindOf = (d) => d.path === "/fanout" ? "fanout"
  : String(d.payload.prompt).startsWith("Classify this task") ? "triage"
  : String(d.payload.prompt).includes("strict, independent reviewer") ? "verify" : "executor";

process.env.ALLOW_NO_AUTH = "1";
process.env.POLICY_PATH = POLICY_PATH;
process.env.RULES_PATH = p("../../router/rules.js");
process.env.SCOPES_PATH = SCOPES_PATH;
process.env.AUDIT_DIR = mkdtempSync(join(tmpdir(), "router-actions-audit-"));
process.env.CONTROL_DIR = mkdtempSync(join(tmpdir(), "router-actions-control-"));

const router = (await import(p("../../router/server.js"))).default;
const actions = (await import(p("../../router/actions.js"))).default;
const { classifyRisk, resolveScope } = router;

const billing = resolveScope("billing");
const assist = resolveScope("billing-assist");
const commons = resolveScope("jane");   // declares no operations

// =====================================================================
// ROUTE-LEVEL — watched failing against the pre-binding router
// =====================================================================

test("operation: invoicing is scored by what the OPERATION does, not the missing verb", () => {
  const r = classifyRisk({ operation: "invoice-customer" }, billing);
  assert.equal(r.dims.access, 3, "send-class operation must score access 3");
  assert.equal(r.controls.requireApproval, true);
});

test("operation: a caller cannot declare itself below the operation", () => {
  const r = classifyRisk({ action: "advise", operation: "invoice-customer" }, billing);
  assert.equal(r.dims.access, 3, "declared advise must not lower a send operation");
  assert.equal(r.binding.underDeclared, true, "the disagreement is recorded, not smoothed over");
});

test("operation: irreversible needs a human even when its verb alone would not", () => {
  // write-class alone is requireVerify, not requireApproval.
  const r = classifyRisk({ operation: "void-draft-invoice" }, billing);
  assert.equal(r.dims.access, 2);
  assert.equal(r.controls.requireApproval, true, "irreversible must force approval");
  assert.ok(["high", "critical"].includes(r.level),
    `irreversible must reach a level where segregation of duties applies; got ${r.level}`);
});

test("operation: naming one the scope never declared is refused, not defaulted", () => {
  assert.throws(() => classifyRisk({ operation: "wire-transfer" }, billing),
    (e) => e.statusCode === 403 && /not declared/.test(e.message));
});

test("operation: declared on a different scope does not travel", () => {
  assert.throws(() => classifyRisk({ operation: "void-draft-invoice" }, assist),
    (e) => e.statusCode === 403);
  assert.throws(() => classifyRisk({ operation: "invoice-customer" }, commons),
    (e) => e.statusCode === 403);
});

test("operation: a declaration that lies about its tools fails before it can run", () => {
  assert.throws(() => classifyRisk({ operation: "lookup-invoice" }, billing),
    (e) => e.statusCode === 500 && /mcp__billing/.test(e.message) && /send/.test(e.message));
});

test("warn mode: legacy verb traffic is scored exactly as before — no silent blast radius", () => {
  // The live dispatcher omits `action`. Under warn it must still score access 1,
  // with the binding recorded beside it rather than applied.
  const r = classifyRisk({}, commons);
  assert.equal(r.dims.access, 1);
  assert.equal(r.binding.bound, false);
  assert.equal(r.binding.declared, null, "an omitted verb is recorded as null, never back-filled");
  assert.equal(r.binding.enforce, "warn");
});

test("route: the mode cap reads the operation, so a human-led scope cannot have an agent invoice", async () => {
  assert.equal(typeof router.route, "function", "route must be exported for gate tests");
  const out = await router.route({
    scope: "billing-assist", operation: "invoice-customer", prompt: "invoice the customer for September",
    approved: true, requester: "alice@example.com", approver: "bob@example.com",
    approverGroups: ["agents-approvers@example.com", "security-approvers@example.com"],
  });
  assert.equal(out.blocked, "mode-forbids-action",
    `human-led caps at write; a send operation must be refused even when approved. got ${out.blocked}`);
});

test("route: an unapproved invoice stops at the approval gate and says why", async () => {
  const out = await router.route({ scope: "billing", operation: "invoice-customer", prompt: "invoice the customer" });
  assert.equal(out.blocked, "approval-required");
  assert.match(out.message, /invoice-customer/, "the refusal names the operation");
});

test("route: the requester cannot approve their own invoice", async () => {
  const out = await router.route({
    scope: "billing", operation: "invoice-customer", prompt: "invoice the customer",
    approved: true, requester: "alice@example.com", approver: "alice@example.com",
    approverGroups: ["agents-approvers@example.com", "security-approvers@example.com"],
  });
  assert.equal(out.blocked, "approval-unauthorized");
});

test("ledger: records declared, governing and effective verbs and the operation", () => {
  const input = { action: "advise", operation: "invoice-customer" };
  const risk = classifyRisk(input, billing);
  const rec = router.auditRecord(input, { decision: { scope: "billing", risk }, blocked: "approval-required" });
  assert.equal(rec.declaredAction, "advise");
  assert.equal(rec.action, "send", "`action` is what the router governed by");
  assert.equal(rec.operation, "invoice-customer");
  assert.equal(rec.actionUnderDeclared, true);
  assert.equal(rec.actionBound, true);
});

test("ledger: an omitted verb is null, not 'advise'", () => {
  const risk = classifyRisk({}, commons);
  const rec = router.auditRecord({}, { decision: { scope: "jane", risk } });
  assert.equal(rec.declaredAction, null);
  assert.equal(rec.action, "advise", "warn mode still governs by the legacy verb");
  assert.equal(rec.actionBound, false);
});

test("dispatch: a bound request sends the narrowed set; an unbound one sends the scope's", () => {
  const bound = classifyRisk({ operation: "invoice-customer" }, billing).binding;
  assert.deepEqual(router.dispatchTools(bound, billing), ["Read", "mcp__billing"]);
  const unbound = classifyRisk({}, commons).binding;
  assert.equal(router.dispatchTools(unbound, commons), undefined, "legacy: the scope declares none");
});

test("dispatch: router-internal calls get NO tools — on a runner that can enforce it", () => {
  // They judge untrusted text — the raw prompt, the executor's output. A prompt
  // injection in either used to land in a session holding Bash.
  assert.deepEqual(router.internalCallTools("claude"), []);
  // The second-vendor runner 422s EVERY tool list until its read-only mode is on.
  assert.equal(router.internalCallTools("gemini"), undefined, "legacy until secondVendorReadOnly");
  const on = policyWith({ secondVendorReadOnly: true });
  assert.deepEqual(router.internalCallTools("gemini", on), []);
  for (const v of ["true", 1, "yes"])
    assert.equal(router.internalCallTools("gemini", policyWith({ secondVendorReadOnly: v })), undefined,
      `only the literal true enables it, not ${JSON.stringify(v)}`);
});

test("vendor: ALL bound work stays on the primary vendor until second-vendor read-only is enabled", () => {
  const send = classifyRisk({ operation: "invoice-customer" }, billing).binding;
  const read = classifyRisk({ operation: "report-receivables" }, billing).binding;
  const unbound = classifyRisk({}, commons).binding;
  // Default: the second vendor would 422 even a read-only list.
  assert.equal(actions.requiresEnforcingVendor(send, policyWith()), true);
  assert.equal(actions.requiresEnforcingVendor(read, policyWith()), true, "read-only too, until enabled");
  // Enabled: only bound work above read-class is kept off it.
  const on = policyWith({ secondVendorReadOnly: true });
  assert.equal(actions.requiresEnforcingVendor(send, on), true);
  assert.equal(actions.requiresEnforcingVendor(read, on), false);
  assert.equal(actions.requiresEnforcingVendor(unbound, policyWith()), false, "unbound traffic keeps today's routing");
});

test("fanout: a caller's per-task tools can narrow but never widen", () => {
  const tasks = [
    { prompt: "a", allowedTools: ["Read", "Bash"] },     // tries to add Bash
    { prompt: "b" },                                      // names nothing
    { prompt: "c", allowedTools: [] },                    // asks for nothing
  ];
  const out = router.fanoutTasks(tasks, ["Read", "Grep"]);
  assert.deepEqual(out[0].allowedTools, ["Read"]);
  assert.deepEqual(out[1].allowedTools, ["Read", "Grep"]);
  assert.deepEqual(out[2].allowedTools, []);
  const legacy = router.fanoutTasks([{ prompt: "d", allowedTools: ["Read"] }], undefined);
  assert.deepEqual(legacy[0].allowedTools, ["Read"], "unbound + unrestricted scope: the caller's narrowing stands");
});

// =====================================================================
// END TO END — what actually reaches a runner
// =====================================================================

const APPROVAL = {
  approved: true, requester: "alice@example.com", approver: "bob@example.com",
  approverGroups: ["agents-approvers@example.com", "security-approvers@example.com"],
};

test("dispatch e2e: an approved invoice reaches the runner with ONLY the operation's tools", async () => {
  const n = DISPATCHED.length;
  const out = await router.route({ scope: "billing", operation: "invoice-customer",
    prompt: "invoice the customer for September", ...APPROVAL });
  assert.equal(out.blocked, undefined, `expected dispatch, got blocked=${out.blocked}: ${out.message}`);
  const sent = dispatchedSince(n);
  const exec = sent.filter((d) => kindOf(d) === "executor");
  assert.equal(exec.length, 1, "exactly one executor dispatch");
  assert.deepEqual(exec[0].payload.allowedTools, ["Read", "mcp__billing"]);
  assert.equal(out.decision.action, "send");
  assert.equal(out.decision.actionBinding.operation, "invoice-customer");
});

test("dispatch e2e: internal calls to the primary vendor are sent NO tools", async () => {
  // `enterprise` is pii:block, so blockCrossVendor escalates triage to claude.
  const n = DISPATCHED.length;
  const out = await router.route({ scope: "enterprise", prompt: "summarise the release notes" });
  assert.equal(out.blocked, undefined, `got ${out.blocked}: ${out.message}`);
  const triage = dispatchedSince(n).filter((d) => kindOf(d) === "triage");
  assert.equal(triage.length, 1);
  assert.equal(triage[0].host, "claude-runner");
  assert.deepEqual(triage[0].payload.allowedTools, [], "triage on claude gets an explicit empty tool list");
});

test("REGRESSION (warn mode): internal calls to the second vendor keep legacy tools — [] would 422 there", async () => {
  // Found in cross-review before merge: `[]` was sent to every internal call, and
  // the second-vendor runner refuses every tool list until its read-only mode is
  // configured. Triage runs there for every untyped request on a pooled scope, so
  // warn mode — meant to change nothing — would have broken untyped routing.
  const n = DISPATCHED.length;
  await router.route({ scope: "billing", operation: "invoice-customer",
    prompt: "invoice the customer for September", ...APPROVAL });
  const sent = dispatchedSince(n);
  const internal = sent.filter((d) => ["triage", "verify"].includes(kindOf(d)));
  assert.ok(sent.some((d) => kindOf(d) === "triage"), "triage ran (no task_type given)");
  assert.ok(sent.some((d) => kindOf(d) === "verify"), "verify ran (send-class requires it)");
  for (const d of internal) {
    assert.equal(d.host, "gemini-runner", `${kindOf(d)} went to the second vendor in this fixture`);
    assert.equal("allowedTools" in d.payload, false,
      `${kindOf(d)} to the second vendor must not carry a tool list; it would be refused`);
  }
  // And the bound executor itself never went there.
  for (const d of sent.filter((x) => kindOf(x) === "executor"))
    assert.equal(d.host, "claude-runner", "bound work is kept on the primary vendor");
});

test("dispatch e2e: unbound legacy traffic is dispatched exactly as before", async () => {
  const n = DISPATCHED.length;
  const out = await router.route({ scope: "jane", prompt: "summarise the release notes" });
  assert.equal(out.blocked, undefined);
  const exec = dispatchedSince(n).filter((d) => kindOf(d) === "executor");
  assert.equal(exec.length, 1);
  assert.equal("allowedTools" in exec[0].payload && exec[0].payload.allowedTools !== undefined, false,
    "a scope that declares no tools still sends none — warn mode moves nothing");
});

test("fanout e2e: an operation's approval gate applies to /fanout too", async () => {
  const n = DISPATCHED.length;
  const out = await router.fanoutRoute({ scope: "billing", operation: "invoice-customer",
    repo: "r", tasks: [{ prompt: "invoice A" }, { prompt: "invoice B" }] });
  assert.equal(out.blocked, "approval-required", "/fanout used to skip every gate");
  assert.equal(dispatchedSince(n).length, 0, "nothing reaches a runner before approval");
});

test("fanout e2e: every task gets the decided tools; a task cannot add Bash", async () => {
  const n = DISPATCHED.length;
  const out = await router.fanoutRoute({ scope: "billing", operation: "report-receivables", repo: "r",
    tasks: [{ prompt: "a", allowedTools: ["Read", "Bash"] }, { prompt: "b" }] });
  assert.equal(out.blocked, undefined, `got ${out.blocked}: ${out.message}`);
  const f = dispatchedSince(n).filter((d) => kindOf(d) === "fanout");
  assert.equal(f.length, 1);
  assert.deepEqual(f[0].payload.tasks[0].allowedTools, ["Read"]);
  assert.deepEqual(f[0].payload.tasks[1].allowedTools, ["Read", "mcp__billing-reports"]);
});

// =====================================================================
// PURE MODULE — the binding rules, with policy injected
// =====================================================================

const BLOCK = policyWith({ actionBinding: "block" });
const WARN = policyWith();
const scope = (controls = {}) => ({ name: "t", controls });

test("enforce: only the literal 'warn' relaxes — missing or misspelt enforces", () => {
  for (const v of [undefined, "block", "Warn", "warm", ""]) {
    const pol = policyWith({ actionBinding: v });
    if (v === undefined) delete pol.risk.enforce.actionBinding;
    assert.equal(actions.bindAction({ action: "read" }, scope(), pol).enforce, "block", `value ${JSON.stringify(v)}`);
  }
  assert.equal(actions.bindAction({ action: "read" }, scope(), WARN).enforce, "warn");
});

test("block: an omitted verb must be named", () => {
  assert.throws(() => actions.bindAction({}, scope(), BLOCK), (e) => e.statusCode === 400 && /required/.test(e.message));
});

test("block: an unknown verb is refused rather than scored as advise", () => {
  assert.throws(() => actions.bindAction({ action: "deploy" }, scope(), BLOCK), (e) => e.statusCode === 400);
});

// `trigger` is the other caller-declared field, and it had the same hole: an
// unknown value fell to policy.risk.autonomy.default (1, a human turn). A lane
// that sent trigger:"schedule" (the key is "scheduled") scored as human-started and
// skipped requireVerify; dispatch.py sends "dispatch". Omitted still means a human
// turn, the documented default; a value nobody defined is refused, like a verb.
test("block: an unknown trigger is refused rather than scored as a human turn", () => {
  for (const t of ["schedule", "dispatch", "cron", ""]) {
    assert.throws(() => actions.bindAction({ action: "read", trigger: t }, scope(), BLOCK),
      (e) => e.statusCode === 400 && /trigger/.test(e.message), `trigger ${JSON.stringify(t)}`);
  }
});

test("block: every trigger the policy defines is accepted, and omitted means a turn", () => {
  const known = Object.keys(BLOCK.risk.autonomy).filter((k) => k !== "default" && !k.startsWith("_"));
  for (const t of known) assert.doesNotThrow(() => actions.bindAction({ action: "read", trigger: t }, scope(), BLOCK), t);
  assert.doesNotThrow(() => actions.bindAction({ action: "read" }, scope(), BLOCK));
});

test("policy: an externally-started unattended run ('event') is not scored as a human turn", () => {
  const A = BLOCK.risk.autonomy;
  assert.ok(A.event > A.turn, "event must outrank a human-initiated turn");
  assert.ok(A.event < A.proactive, "event is not agent-initiated; proactive stays the top");
});

test("warn: an unknown trigger is not refused, but it is recorded", () => {
  const b = actions.bindAction({ action: "read", trigger: "dispatch" }, scope(), WARN);
  assert.ok(b.notes.some((n) => /unknown trigger "dispatch"/.test(n)), JSON.stringify(b.notes));
});

test("block: advise on an unrestricted scope narrows to read-and-below only", () => {
  const b = actions.bindAction({ action: "advise" }, scope(), BLOCK);
  assert.equal(b.bound, true);
  for (const t of ["Bash", "Edit", "Write", "WebFetch", "mcp__n8n", "mcp__billing"])
    assert.ok(!b.narrowing.tools.includes(t), `${t} must not reach an advise request`);
  for (const t of ["TodoWrite", "mcp__ask-human"])
    assert.ok(b.narrowing.tools.includes(t), `${t} is advise-class and should remain`);
});

test("warn: the narrowing is computed and recorded, but the legacy verb governs", () => {
  const b = actions.bindAction({ action: "advise" }, scope(), WARN);
  assert.equal(b.bound, false);
  assert.equal(actions.governingVerb(b), "advise");
  assert.ok(b.narrowing.available && b.narrowing.dropped.includes("Bash"), "what binding WOULD cut is visible");
});

test("narrowing: an operation can pick from the scope's tools, never add to them", () => {
  const s = scope({ allowedTools: ["Read", "Grep"], operations: { op: { access: "send", tools: ["Read", "mcp__billing"] } } });
  const b = actions.bindAction({ operation: "op" }, s, WARN);
  assert.deepEqual(b.narrowing.tools, ["Read"], "mcp__billing is not in the scope's set");
});

test("narrowing: an unclassified tool is treated as able to do anything", () => {
  const s = scope({ allowedTools: ["Read", "SomeNewTool"] });
  const b = actions.bindAction({ action: "write" }, s, BLOCK);
  assert.deepEqual(b.narrowing.tools, ["Read"]);
  assert.deepEqual(b.narrowing.dropped, ["SomeNewTool"]);
});

test("narrowing: bound with no toolAccess configured reports unavailable rather than guessing", () => {
  const pol = policyWith({ actionBinding: "block" }); delete pol.risk.toolAccess;
  const b = actions.bindAction({ action: "read" }, scope(), pol);
  assert.equal(b.narrowing.available, false);
});

test("declarations: only enforceable tool names are accepted", () => {
  const probs = (tools) => actions.operationProblems("op", { access: "execute", tools }, WARN);
  assert.equal(probs(["Read", "mcp__billing"]).length, 0);
  assert.match(probs(["Bash(git log:*)"]).join(), /specifier/);
  assert.match(probs(["mcp__billing__create_invoice"]).join(), /whole servers/);
});

test("declarations: a verb raised above the operation is honoured", () => {
  const s = scope({ operations: { op: { access: "read", tools: ["Read"] } } });
  const b = actions.bindAction({ action: "write", operation: "op" }, s, WARN);
  assert.equal(b.effective, "write", "a caller may ask for MORE scrutiny, never less");
});
