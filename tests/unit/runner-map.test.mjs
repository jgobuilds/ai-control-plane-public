// scope -> runner host routing (ISOLATION-FIX-PLAN phase 1, step 3).
//
// gen_compose.py has written context/runner-map.json since phase 1 and the
// router never read it. Dispatch honoured `runnerHost` for `isolation: "silo"`
// scopes only, so every POOL scope went to the SHARED runner with a cwd. That
// is what "pool isolation is advisory (cwd only)" means in the threat model:
// a cwd is a suggestion to a process that can read the whole mount.
//
// RUNNER_MAP is read at import, so every case reloads the module in a child
// process. Setting it after the module cached would test the previous value.
//
//   node --test tests/unit/runner-map.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// The router reads policy/rules/scopes/audit paths from the environment at
// import, defaulting to /app/* — correct in the image, absent in a checkout.
// router.test.mjs already sets these; this one did not, and CI found it on the
// first run after the node tests were wired in. Which is the point of wiring
// them in.
const p = (rel) => fileURLToPath(new URL(rel, import.meta.url));
const AUDIT_DIR = mkdtempSync(join(tmpdir(), "runner-map-audit-"));
const SERVER = process.env.ROUTER_SERVER || p("../../router/server.js");
const ENV = {
  ...process.env,
  ALLOW_NO_AUTH: "1",
  ROUTER_TOKEN: "test",
  POLICY_PATH: process.env.POLICY_PATH || p("../../router/policy.json"),
  RULES_PATH: process.env.RULES_PATH || p("../../router/rules.js"),
  SCOPES_PATH: process.env.SCOPES_PATH || p("../../context/scopes.json"),
  RUNNER_MAP_PATH: process.env.RUNNER_MAP_PATH || p("../../context/runner-map.json"),
  AUDIT_DIR,
};

const SILO = { name: "tenant-a", controls: { isolation: "silo" },
               node: { runnerHost: "claude-runner-tenant-a" } };
const POOL = { name: "jane", controls: { isolation: "pool" }, node: {} };

function target(mapOn, scope, provider, opts = {}) {
  const script = `
    process.env.RUNNER_MAP = ${JSON.stringify(mapOn ? "on" : "off")};
    const m = require(${JSON.stringify(SERVER)});
    try { console.log("OK:" + m.targetFor(${JSON.stringify(scope)}, ${JSON.stringify(provider)}, ${JSON.stringify(opts)})); }
    catch (e) { console.log("THROW:" + e.message); }
  `;
  return execFileSync(process.execPath, ["-e", script], { encoding: "utf8", env: ENV }).trim();
}

test("OFF: behaviour is exactly what it was — pool goes to the shared runner", () => {
  // The flag's whole purpose is that turning it on is the only change. If OFF
  // differed at all, cutover would be entangled with a behaviour change.
  assert.equal(target(false, SILO, "claude"), "OK:claude-runner-tenant-a");
  assert.equal(target(false, POOL, "claude"), "OK:claude");
  assert.equal(target(false, POOL, "gemini"), "OK:gemini");
});

test("ON: a POOL scope moves to its root's runner — the actual fix", () => {
  // jane is pool, so pre-fix it shared a container with every other pool scope
  // and was separated only by cwd.
  assert.equal(target(true, POOL, "claude"), "OK:claude-runner-engineering");
});

test("ON: a silo scope still lands on its dedicated runner", () => {
  assert.equal(target(true, SILO, "claude"), "OK:claude-runner-tenant-a");
});

test("ON: a non-claude provider goes to a SCOPED second-vendor runner, or nowhere", () => {
  // This assertion used to be `"OK:gemini"` — the bare provider name, which
  // callRunner resolved to the shared `gemini-runner`: one container, the whole
  // workspace mounted, no SCOPE_ROOT. So the test was green while encoding the
  // C1/C2 residual as the expected result.
  //
  // gemini is now pool-only and per-topology. The SAMPLE tree has no pooled
  // scopes (every scope sits under an isolation root), so it emits no gemini
  // runner and providerHosts.gemini is null — meaning there is no second-vendor
  // path here at all, and refusing is correct. route() escalates to the cheapest
  // permitted claude tier before ever reaching this, so the refusal is a
  // backstop rather than the user-visible behaviour (see escalateForVendor in
  // router.test.mjs).
  const out = target(true, POOL, "gemini", { crossVendorBlocked: false });
  assert.match(out, /^THROW:/, out);
  assert.doesNotMatch(out, /OK:gemini$/, out);
});

test("ON: blockCrossVendor is enforced on the DISPATCH path, not just verify", () => {
  // The hole this closes: `blockCrossVendor` is documented as "don't send
  // confidential work to a 2nd vendor" and was read only in the verify branch.
  // A pii:block scope asking for a t1 task went to gemini as its PRIMARY runner
  // with the flag computed true and ignored — confirmed against the live router.
  const out = target(true, POOL, "gemini", { crossVendorBlocked: true });
  assert.match(out, /^THROW:/, out);
  assert.match(out, /blockCrossVendor/, out);
});

test("OFF: the flag changes nothing when the map is off", () => {
  // Cutover must be the only variable. blockCrossVendor enforcement rides with
  // RUNNER_MAP so flipping it is one decision, not two entangled ones.
  assert.equal(target(false, POOL, "gemini", { crossVendorBlocked: true }), "OK:gemini");
});

test("ON: an isolation ROOT never gets a second-vendor runner", () => {
  // The pool-only rule, asserted at the boundary. A root exists because of
  // `silo` or `pii:block`, and both put the risk model's data dimension at 3 —
  // exactly where blockCrossVendor fires. So a root reaching a second vendor is
  // a state policy already forbids, and the topology must not offer a path to
  // it even if some caller passes crossVendorBlocked:false.
  const out = target(true, SILO, "gemini", { crossVendorBlocked: false });
  assert.match(out, /^THROW:/, out);
});

test("ON: an unmapped scope FAILS rather than falling back", () => {
  // The fallback is the bypass. A scope missing from the map must stop the
  // request, not quietly get the shared runner.
  const out = target(true, { name: "nope", controls: {}, node: {} }, "claude");
  assert.match(out, /^THROW:.*no runner-map entry/, out);
  assert.doesNotMatch(out, /OK:/, out);
});

test("ON: an unscoped call is unaffected", () => {
  // Internal calls without a scope (health probes, admin paths) must not start
  // throwing the moment the flag flips.
  assert.equal(target(true, null, "claude"), "OK:claude");
});
