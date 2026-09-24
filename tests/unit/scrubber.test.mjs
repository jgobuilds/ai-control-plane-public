// Unit tests for the scrubber's exported pure functions — the PII tokenization
// chokepoint (CONTEXT-ARCHITECTURE.md: runners see «EMAIL_1», never the value).
// Runnable in CI:  node --test tests/unit/scrubber.test.mjs
//
// VAULT_DIR/WORKSPACE_DIR are pointed at temp dirs so the deterministic token
// vault can be written without the container's /vault mount. SCOPES_PATH points
// at the real registry (the module validates scopes at import).
import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const p = (rel) => fileURLToPath(new URL(rel, import.meta.url));
process.env.ALLOW_NO_AUTH = "1";
process.env.SCOPES_PATH = p("../../context/scopes.json");
process.env.VAULT_DIR = mkdtempSync(join(tmpdir(), "scrub-vault-"));
process.env.WORKSPACE_DIR = mkdtempSync(join(tmpdir(), "scrub-ws-"));

const scrubber = (await import(p("../../scrubber/server.js"))).default;
const { scrub, rehydrate, resolveScope, ingest } = scrubber;

test("scrub: same value -> same token, deterministically (equality preserved)", () => {
  const { text, findings } = scrub("Email ada@lab.io or ada@lab.io again", "jane");
  const tokens = text.match(/«EMAIL_\d+»/g);
  assert.equal(tokens.length, 2);
  assert.equal(tokens[0], tokens[1]);        // identical value -> identical token
  assert.equal(findings.EMAIL, 2);
});

test("scrub: same value keeps its token across separate calls (vault persists)", () => {
  const first = scrub("reach grace@lab.io", "jane").text.match(/«EMAIL_\d+»/)[0];
  const second = scrub("again grace@lab.io", "jane").text.match(/«EMAIL_\d+»/)[0];
  assert.equal(first, second);
});

test("scrub: counts each structured PII type", () => {
  const { findings } = scrub("Call 555-123-4567, SSN 123-45-6789, mail ivy@lab.io", "jane");
  assert.equal(findings.PHONE, 1);
  assert.equal(findings.SSN, 1);
  assert.equal(findings.EMAIL, 1);
});

test("scrub: benign text yields no findings and is unchanged", () => {
  const { text, findings } = scrub("The quarterly report is ready for review.", "jane");
  assert.deepEqual(findings, {});
  assert.equal(text, "The quarterly report is ready for review.");
});

test("rehydrate: round-trips a scrubbed value back to the original", () => {
  const scoped = "jane";
  const email = "mallory@lab.io";
  const token = scrub(`contact ${email}`, scoped).text.match(/«EMAIL_\d+»/)[0];
  const { text, restored } = rehydrate(`please contact ${token}`, scoped);
  assert.equal(text, `please contact ${email}`);
  assert.equal(restored, 1);
});

test("rehydrate: unknown token passes through unchanged (loud, not silently wrong)", () => {
  const { text, restored } = rehydrate("see «EMAIL_9999» and «SSN_42»", "jane");
  assert.equal(text, "see «EMAIL_9999» and «SSN_42»");
  assert.equal(restored, 0);
});

test("resolveScope: unknown scope throws", () => {
  assert.throws(() => resolveScope("no-such-scope"), /unknown scope/);
});

test("resolveScope: known scope returns its ancestry chain", () => {
  const s = resolveScope("tenant-a");
  assert.deepEqual(s.chain, ["enterprise", "consulting", "tenant-a"]);
});

test("ingest: refuses a silo-isolated scope (must use its dedicated runner)", async () => {
  // tenant-a is isolation:"silo" — its data must never land in the shared workspace.
  //
  // `assert.rejects` + await, NOT `assert.throws`. ingest() is async, so it
  // returns a rejected promise rather than throwing synchronously; assert.throws
  // therefore asserted nothing and the rejection escaped as an unhandledRejection
  // AFTER the test had already reported. Node says so explicitly: it "would have
  // caused the test to fail, but instead triggered an unhandledRejection event".
  //
  // So this test — guarding the rule that a silo tenant's data must never reach
  // the shared workspace — was not testing it. Invisible until the node suites
  // ran in CI for the first time, because nothing ran them.
  await assert.rejects(() => ingest("tenant-a", "notes.md", "some text"), /silo-isolated/);
});
