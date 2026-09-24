// Unit tests for the router's exported pure functions (threat-model: the router
// IS the enforcement chokepoint, so its helpers get direct coverage).
// Runnable in CI:  node --test tests/unit/router.test.mjs
//
// The router module fail-closes at import if ROUTER_TOKEN/RUNNER_TOKEN are unset
// (unless ALLOW_NO_AUTH=1), and it reads policy/rules/scopes/audit paths from env
// at import time. We set those to the repo's real artifacts + a temp audit dir
// BEFORE the dynamic import, so importing the CJS module never binds a port
// (require.main guard) and never touches /app or /audit.
import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";

const p = (rel) => fileURLToPath(new URL(rel, import.meta.url));
const AUDIT_DIR = mkdtempSync(join(tmpdir(), "router-audit-"));

process.env.ALLOW_NO_AUTH = "1";                       // don't process.exit at import
process.env.POLICY_PATH = p("../../router/policy.json");
process.env.RULES_PATH = p("../../router/rules.js");
process.env.SCOPES_PATH = p("../../context/scopes.json");
process.env.AUDIT_DIR = AUDIT_DIR;

const router = (await import(p("../../router/server.js"))).default;
const { tokenOk, classifyRisk, resolveMode, resolveScope, clampTier, escalateForVendor,
        targetFor, verifyOutcome, runnerErrorSummary, auditRecord } = router;

// ---------- tokenOk (constant-time, fail-closed) ----------
test("tokenOk: equal tokens pass", () => {
  assert.equal(tokenOk("s3cret-token", "s3cret-token"), true);
});
test("tokenOk: unequal tokens (same length) fail", () => {
  assert.equal(tokenOk("s3cret-tokeX", "s3cret-token"), false);
});
test("tokenOk: length mismatch fails (no timingSafeEqual throw)", () => {
  assert.equal(tokenOk("short", "s3cret-token"), false);
});
test("tokenOk: empty expected fails closed unless ALLOW_NO_AUTH=1", () => {
  const prev = process.env.ALLOW_NO_AUTH;
  process.env.ALLOW_NO_AUTH = "1";
  assert.equal(tokenOk("anything", ""), true);     // dev bypass ON
  process.env.ALLOW_NO_AUTH = "0";
  assert.equal(tokenOk("anything", ""), false);    // fail-closed when OFF
  process.env.ALLOW_NO_AUTH = prev;
});

// ---------- classifyRisk (data x access x autonomy matrix) ----------
const siloScope = resolveScope("tenant-a"); // isolation:silo  -> data=3
const warnScope = resolveScope("jane");     // personal pii:warn -> data=2

test("classifyRisk: silo + advise + turn => high", () => {
  const r = classifyRisk({ action: "advise", trigger: "turn" }, siloScope);
  assert.equal(r.level, "high");
});
test("classifyRisk: pii:warn + write + turn => medium", () => {
  const r = classifyRisk({ action: "write", trigger: "turn" }, warnScope);
  assert.equal(r.level, "medium");
});
test("classifyRisk: apply + proactive => critical (two dims >=3)", () => {
  const r = classifyRisk({ action: "apply", trigger: "proactive" }, warnScope);
  assert.equal(r.level, "critical");
});
test("classifyRisk: confidential data (>=3) sets blockCrossVendor", () => {
  const r = classifyRisk({ action: "advise", trigger: "turn" }, siloScope);
  assert.equal(r.controls.blockCrossVendor, true);
});
test("classifyRisk: non-confidential data does NOT block cross-vendor", () => {
  const r = classifyRisk({ action: "advise", trigger: "turn" }, warnScope);
  assert.equal(r.controls.blockCrossVendor, false);
});
test("classifyRisk: apply/proactive forces approval + verify", () => {
  const r = classifyRisk({ action: "apply", trigger: "proactive" }, warnScope);
  assert.equal(r.controls.requireApproval, true);
  assert.equal(r.controls.requireVerify, true);
});

// ---------- resolveMode (scope floor; request can only tighten) ----------
test("resolveMode: scope's declared mode is the floor", () => {
  // tenant-a declares mode:"ai-led"; no request mode -> stays ai-led.
  const m = resolveMode({}, siloScope);
  assert.equal(m.name, "ai-led");
});
test("resolveMode: a request can only TIGHTEN, never relax", () => {
  // request tries to relax ai-led -> ai-only: ignored (floor holds).
  const relaxed = resolveMode({ mode: "ai-only" }, siloScope);
  assert.equal(relaxed.name, "ai-led");
  // request tightens ai-led -> human-led: applied.
  const tightened = resolveMode({ mode: "human-led" }, siloScope);
  assert.equal(tightened.name, "human-led");
});
test("resolveMode: human-only caps the agent at advise", () => {
  const m = resolveMode({ mode: "human-only" }, warnScope);
  assert.equal(m.name, "human-only");
  assert.equal(m.maxAction, "advise");
});

// ---------- clampTier (cost ceiling) ----------
// Default policy maxTier is t3, so tiers within the ceiling pass unchanged; this
// asserts the non-clamping identity + allowFrontier plumbing. (The clamp branch —
// tier above ceiling — is exercised by conformance when maxTier is lowered.)
test("clampTier: tier within ceiling is not clamped", () => {
  const r = clampTier("t2", { allowFrontier: false });
  assert.deepEqual(r, { tier: "t2", clamped: false });
});
test("clampTier: frontier tier at the ceiling is allowed", () => {
  const r = clampTier("t3", { allowFrontier: false });
  assert.equal(r.clamped, false);
  assert.equal(r.tier, "t3");
});

// ---------- escalateForVendor (blockCrossVendor: escalate, don't refuse) ----------
// The bug this replaces: a pii:block scope asking for a t1 task type got a 403
// and nothing else, so the scopes doing confidential work lost the cheap tier
// entirely. These assert the escalation happens AND that it never silently
// downgrades capability.
test("escalateForVendor: unblocked scope is untouched", () => {
  assert.deepEqual(escalateForVendor("t1", { crossVendorBlocked: false }),
    { tier: "t1", escalatedFrom: null });
});
test("escalateForVendor: blocked t1 escalates to the cheapest permitted tier", () => {
  const r = escalateForVendor("t1", { crossVendorBlocked: true });
  assert.equal(r.escalatedFrom, "t1");
  assert.equal(r.tier, "t2");
});
test("escalateForVendor: a tier already on the primary vendor does not escalate", () => {
  // Otherwise every blocked request would drift upward one tier per pass.
  assert.deepEqual(escalateForVendor("t2", { crossVendorBlocked: true }),
    { tier: "t2", escalatedFrom: null });
  assert.deepEqual(escalateForVendor("t3", { crossVendorBlocked: true }),
    { tier: "t3", escalatedFrom: null });
});
test("escalateForVendor: deterministic t0 is never escalated (it calls no vendor)", () => {
  assert.deepEqual(escalateForVendor("t0", { crossVendorBlocked: true }),
    { tier: "t0", escalatedFrom: null });
});
test("escalateForVendor: never resolves DOWNWARD", () => {
  // "Cheapest permitted" searched over the whole table could land below the
  // resolved tier and answer a hard question with a weaker model. The extra
  // spend of escalating is visible in the ledger; a capability downgrade is not.
  const ORDER = ["t0", "t1", "t2", "t3"];
  for (const t of ORDER) {
    const r = escalateForVendor(t, { crossVendorBlocked: true });
    assert.ok(ORDER.indexOf(r.tier) >= ORDER.indexOf(t),
      `${t} escalated down to ${r.tier}`);
  }
});
test("escalateForVendor: the escalated tier is one targetFor will actually accept", () => {
  // The two must agree by construction. If escalation picked a tier the
  // dispatch guard still refuses, the scope gets a 403 after paying for the
  // more expensive route — worse than the refusal it replaced.
  const r = escalateForVendor("t1", { crossVendorBlocked: true });
  const policy = JSON.parse(readFileSync(p("../../router/policy.json"), "utf8"));
  const scope = resolveScope("jane");
  assert.doesNotThrow(() => targetFor(scope, policy.tiers[r.tier].provider,
    { crossVendorBlocked: true }));
});

// ---------- verifyOutcome (a dead reviewer must not destroy live work) ----------
// The bug: a cross-vendor verify whose vendor was down threw out of route(), so
// the primary work — already run, already paid for — was discarded and the
// caller got a 502. One unauthenticated second vendor took down every t3 request
// in every non-confidential scope.
//
// These branches are near-unreachable live (the same-vendor fallback targets the
// SAME runner that just produced the result, so if the work succeeded the
// fallback almost certainly does too). Near-unreachable is exactly what rots, and
// one branch here is a fail-closed risk control.
test("verifyOutcome: a working verifier is just ok", () => {
  assert.deepEqual(verifyOutcome({ required: false, crossError: null }), { status: "ok" });
  assert.deepEqual(verifyOutcome({ required: true, crossError: null }), { status: "ok" });
});
test("verifyOutcome: cross-vendor down, same-vendor works => degraded, never blocked", () => {
  for (const required of [false, true]) {
    const r = verifyOutcome({ required, crossError: "ENOTFOUND", fallbackTried: true, fallbackError: null });
    assert.equal(r.status, "degraded-same-vendor");
    assert.equal(r.blocked, undefined, "a successful fallback must never block");
  }
});
test("verifyOutcome: both down + verify NOT required => degrade, return the work", () => {
  const r = verifyOutcome({ required: false, crossError: "ENOTFOUND",
                            fallbackTried: true, fallbackError: "ECONNREFUSED" });
  assert.equal(r.status, "unavailable");
  assert.equal(r.blocked, undefined, "a quality heuristic must not destroy completed work");
  assert.match(r.why, /fallback also failed/);
});
test("verifyOutcome: both down + verify REQUIRED => fail closed", () => {
  // The risk model demanded a check. Returning unverified work because the
  // checker was down is precisely what the control exists to prevent.
  const r = verifyOutcome({ required: true, crossError: "ENOTFOUND",
                            fallbackTried: true, fallbackError: "ECONNREFUSED" });
  assert.equal(r.status, "unavailable");
  assert.equal(r.blocked, "verify-unavailable");
});
test("verifyOutcome: no fallback attempted (same-vendor verify) still resolves", () => {
  // blockCrossVendor scopes verify on their OWN vendor, so there is no second
  // vendor to fall back to — the outcome must not depend on a fallback running.
  assert.equal(verifyOutcome({ required: false, crossError: "boom", fallbackTried: false }).status,
               "unavailable");
  assert.equal(verifyOutcome({ required: true, crossError: "boom", fallbackTried: false }).blocked,
               "verify-unavailable");
});

// ---------- runnerErrorSummary (no prompt may reach the ledger) ----------
// The ledger stores promptSha256 and NEVER the prompt. Propagating runner errors
// (finding D1) broke that: a failing CLI reports `Command failed: claude -p <the
// whole prompt> --flags`, that became the Error message, and route() writes the
// message into `blocked`. Two records in the live ledger carried a real prompt
// before this function existed — and an append-only hash chain cannot un-write.
const LEAKED = "Command failed: claude -p Which department should handle a request "
  + "about updating a shipping address? --output-format json --max-turns 40 "
  + "--dangerously-skip-permissions\nWarning: no stdin data received in 3s";

test("runnerErrorSummary: strips the prompt from the exact string that leaked", () => {
  const out = runnerErrorSummary(LEAKED);
  assert.doesNotMatch(out, /shipping address/, out);
  assert.match(out, /<prompt omitted>/);
});
test("runnerErrorSummary: same for the gemini form", () => {
  const out = runnerErrorSummary("Command failed: gemini -p SECRET TEXT --yolo");
  assert.doesNotMatch(out, /SECRET TEXT/, out);
});
test("runnerErrorSummary: keeps only the first line — stderr tails hide prompts", () => {
  assert.equal(runnerErrorSummary("boom\nclaude -p leaky"), "boom");
});
test("runnerErrorSummary: a non-prompt error still diagnoses", () => {
  // Over-scrubbing would be its own failure: the gemini auth error is how the
  // operator learns GEMINI_API_KEY is unset.
  const out = runnerErrorSummary('Please set an Auth method or specify GEMINI_API_KEY');
  assert.match(out, /GEMINI_API_KEY/);
});
test("runnerErrorSummary: capped, and safe on empty/null", () => {
  assert.ok(runnerErrorSummary("x".repeat(5000)).length <= 200);
  assert.equal(runnerErrorSummary(null), "");
  assert.equal(runnerErrorSummary(undefined), "");
});

// ---------- audit hash-chain (must match scripts/verify_audit.py) ----------
test("auditRecord: builds a sha256(prevHash+json) chain verify_audit.py accepts", () => {
  auditRecord({ prompt: "hello", action: "advise", trigger: "turn" },
    { decision: { scope: "jane", tier: "t2" } });
  auditRecord({ prompt: "second", action: "write", trigger: "turn" },
    { decision: { scope: "tenant-a", tier: "t3" } });

  const sha256 = (s) => createHash("sha256").update(s).digest("hex");
  const text = readFileSync(join(AUDIT_DIR, "decisions.jsonl"), "utf8").trimEnd();
  const lines = text.split("\n");
  assert.equal(lines.length, 2);

  // Re-walk exactly like verify_audit.py: hash = sha256(prev + rawJson),
  // and each record's prevHash must equal the running head.
  let prev = "GENESIS";
  for (const line of lines) {
    const sp = line.indexOf(" ");
    const h = line.slice(0, sp);
    const j = line.slice(sp + 1);
    assert.equal(sha256(prev + j), h, "chain hash mismatch");
    assert.equal(JSON.parse(j).prevHash, prev, "prevHash mismatch");
    prev = h;
  }
});
