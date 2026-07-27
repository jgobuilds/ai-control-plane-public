// Unit tests for the runner guardrails: safeCwd (workspace-escape prevention) and
// sanitizeAllowedTools (flag-injection / shell-metachar rejection). These are the
// two pure security helpers the router-supplied inputs flow through.
// Runnable in CI (Linux):  node --test tests/unit/runner.test.mjs
//
// safeCwd resolves against the hardcoded "/workspace" base. We DON'T need that
// dir to exist for the security assertions: the escape check runs BEFORE the
// existence check, so an in-workspace-but-missing path throws "does not exist"
// (proving it PASSED the boundary) while an escaping path throws "escapes".
// POSIX path semantics assumed (CI runs on Linux).
import { test } from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

const p = (rel) => fileURLToPath(new URL(rel, import.meta.url));
process.env.ALLOW_NO_AUTH = "1"; // don't process.exit at import (token unset)

const claude = (await import(p("../../claude-runner/server.js"))).default;
const gemini = (await import(p("../../gemini-runner/server.js"))).default;
const { safeCwd, sanitizeAllowedTools } = claude;

// ---------- safeCwd ----------
test("safeCwd: empty/undefined returns the workspace root", () => {
  assert.equal(safeCwd(), "/workspace");
  assert.equal(safeCwd(""), "/workspace");
});
test("safeCwd: rejects a ../ escape out of the workspace", () => {
  assert.throws(() => safeCwd("../etc/passwd"), /escapes the workspace/);
  assert.throws(() => safeCwd("../../root/.ssh/id_rsa"), /escapes the workspace/);
});
test("safeCwd: an absolute path outside the workspace escapes", () => {
  assert.throws(() => safeCwd("/etc/shadow"), /escapes the workspace/);
});
test("safeCwd: an in-workspace path is accepted past the boundary", () => {
  // Missing dir -> "does not exist" (NOT "escapes") proves it cleared the
  // boundary check and was treated as in-workspace.
  assert.throws(() => safeCwd("scopes/enterprise/consulting/client-a"), /does not exist/);
});

// ---------- sanitizeAllowedTools ----------
test("sanitizeAllowedTools: null passes through as null", () => {
  assert.equal(sanitizeAllowedTools(null), null);
});
test("sanitizeAllowedTools: accepts plain tool tokens", () => {
  assert.deepEqual(sanitizeAllowedTools(["Read", "Grep", "Edit", "Bash"]),
    ["Read", "Grep", "Edit", "Bash"]);
});
test("sanitizeAllowedTools: accepts Claude Code specifiers", () => {
  assert.deepEqual(sanitizeAllowedTools(["Bash(git log:*)", "mcp__srv__tool"]),
    ["Bash(git log:*)", "mcp__srv__tool"]);
});
test("sanitizeAllowedTools: rejects a leading '-' (flag injection)", () => {
  assert.throws(() => sanitizeAllowedTools(["--dangerously-skip-permissions"]),
    /may not start with '-'/);
});
test("sanitizeAllowedTools: rejects shell metacharacters", () => {
  assert.throws(() => sanitizeAllowedTools(["Bash; rm -rf /"]), /disallowed characters/);
  assert.throws(() => sanitizeAllowedTools(["Read && curl evil"]), /disallowed characters/);
  assert.throws(() => sanitizeAllowedTools(["$(whoami)"]), /disallowed characters/);
});
test("sanitizeAllowedTools: rejects a non-array", () => {
  assert.throws(() => sanitizeAllowedTools("Read"), /must be an array/);
});
test("sanitizeAllowedTools: rejects empty/non-string entries", () => {
  assert.throws(() => sanitizeAllowedTools([""]), /non-empty strings/);
  assert.throws(() => sanitizeAllowedTools([42]), /non-empty strings/);
});
test("sanitizeAllowedTools: an empty list collapses to null", () => {
  assert.equal(sanitizeAllowedTools([]), null);
});

// ---------- gemini-runner shares the safeCwd contract ----------
test("gemini safeCwd: same escape prevention", () => {
  assert.equal(gemini.safeCwd(), "/workspace");
  assert.throws(() => gemini.safeCwd("../escape"), /escapes the workspace/);
});
