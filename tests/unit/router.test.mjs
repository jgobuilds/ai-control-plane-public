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
const { tokenOk, classifyRisk, resolveMode, resolveScope, clampTier, auditRecord } = router;

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
const siloScope = resolveScope("client-a"); // isolation:silo  -> data=3
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
  // client-a declares mode:"ai-led"; no request mode -> stays ai-led.
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

// ---------- audit hash-chain (must match scripts/verify_audit.py) ----------
test("auditRecord: builds a sha256(prevHash+json) chain verify_audit.py accepts", () => {
  auditRecord({ prompt: "hello", action: "advise", trigger: "turn" },
    { decision: { scope: "jane", tier: "t2" } });
  auditRecord({ prompt: "second", action: "write", trigger: "turn" },
    { decision: { scope: "client-a", tier: "t3" } });

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
