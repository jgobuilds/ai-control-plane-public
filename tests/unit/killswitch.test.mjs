// Unit tests for the kill switch + budget/rate circuit breaker.
// Runnable in CI:  node --test tests/unit/killswitch.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { writeFileSync, mkdtempSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import ks from "../../router/killswitch.js";
const { isHalted, breaker } = ks;

// ---------- isHalted ----------
test("isHalted: global halt stops everything", () => {
  const r = isHalted({ global: true, scopes: [], reason: "incident" }, ["enterprise", "consulting", "tenant-a"]);
  assert.equal(r.halted, true);
  assert.match(r.reason, /global halt/i);
});

test("isHalted: exact scope match halts", () => {
  const r = isHalted({ global: false, scopes: ["tenant-a"] }, ["enterprise", "consulting", "tenant-a"]);
  assert.equal(r.halted, true);
  assert.match(r.reason, /tenant-a/);
});

test("isHalted: ancestor in the chain halts the descendant", () => {
  const r = isHalted({ global: false, scopes: ["consulting"] }, ["enterprise", "consulting", "tenant-a"]);
  assert.equal(r.halted, true);
  assert.match(r.reason, /consulting/);
});

test("isHalted: no match => not halted", () => {
  const r = isHalted({ global: false, scopes: ["tenant-b"] }, ["enterprise", "consulting", "tenant-a"]);
  assert.equal(r.halted, false);
});

// ---------- breaker ----------
function ledger(recs) {
  const dir = mkdtempSync(join(tmpdir(), "ks-"));
  const p = join(dir, "decisions.jsonl");
  // Mirrors the router's ledger line format: "<hash> <json>".
  writeFileSync(p, recs.map((r) => "HASH " + JSON.stringify(r)).join("\n") + "\n");
  return p;
}

test("breaker: trips when the global count meets the ceiling", () => {
  const now = new Date().toISOString();
  const recs = Array.from({ length: 5 }, () => ({ ts: now, scope: "jane" }));
  const r = breaker({ limits: { windowMinutes: 10, maxRequestsGlobal: 5, maxRequestsPerScope: 100 } }, ledger(recs), "jane");
  assert.equal(r.tripped, true);
  assert.match(r.reason, /global ceiling/);
});

test("breaker: trips on the per-scope ceiling", () => {
  const now = new Date().toISOString();
  const recs = Array.from({ length: 4 }, () => ({ ts: now, scope: "tenant-a" }));
  const r = breaker({ limits: { windowMinutes: 10, maxRequestsGlobal: 100, maxRequestsPerScope: 4 } }, ledger(recs), "tenant-a");
  assert.equal(r.tripped, true);
  assert.match(r.reason, /per-scope ceiling/);
});

test("breaker: records outside the window don't count", () => {
  const old = new Date(Date.now() - 60 * 60 * 1000).toISOString(); // 1h ago
  const recs = Array.from({ length: 10 }, () => ({ ts: old, scope: "jane" }));
  const r = breaker({ limits: { windowMinutes: 10, maxRequestsGlobal: 5, maxRequestsPerScope: 100 } }, ledger(recs), "jane");
  assert.equal(r.tripped, false);
});

test("breaker: under the ceiling does not trip", () => {
  const now = new Date().toISOString();
  const recs = Array.from({ length: 3 }, () => ({ ts: now, scope: "jane" }));
  const r = breaker({ limits: { windowMinutes: 10, maxRequestsGlobal: 5, maxRequestsPerScope: 5 } }, ledger(recs), "jane");
  assert.equal(r.tripped, false);
});

test("breaker: no limits config => never trips", () => {
  const r = breaker({}, ledger([{ ts: new Date().toISOString(), scope: "jane" }]), "jane");
  assert.equal(r.tripped, false);
});
