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
const { safeCwd, sanitizeAllowedTools, toolArgs } = claude;

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
  assert.throws(() => safeCwd("scopes/enterprise/consulting/tenant-a"), /does not exist/);
});

// ---------- sanitizeAllowedTools ----------
test("sanitizeAllowedTools: null passes through as null", () => {
  assert.equal(sanitizeAllowedTools(null), null);
});
test("sanitizeAllowedTools: accepts plain tool tokens", () => {
  assert.deepEqual(sanitizeAllowedTools(["Read", "Grep", "Edit", "Bash"]),
    ["Read", "Grep", "Edit", "Bash"]);
});
test("sanitizeAllowedTools: REFUSES specifiers it cannot enforce (422)", () => {
  // "Bash(git log:*)" is a permission rule. Under --dangerously-skip-permissions
  // permission rules do nothing, and --tools only takes built-in tool NAMES — so
  // accepting it would either widen to all of Bash or restrict nothing.
  assert.throws(() => sanitizeAllowedTools(["Bash(git log:*)"]),
    (e) => e.statusCode === 422 && /cannot be enforced/.test(e.message));
});
test("sanitizeAllowedTools: REFUSES tool-level MCP names — narrowing is per server", () => {
  assert.throws(() => sanitizeAllowedTools(["mcp__srv__tool"]),
    (e) => e.statusCode === 422 && /mcp__srv/.test(e.message));
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
test("sanitizeAllowedTools: an empty list means NO tools, not the defaults", () => {
  // It used to collapse to null, i.e. "pass no flag", i.e. every default tool —
  // so the most restrictive list anyone could write was the least restrictive.
  assert.deepEqual(sanitizeAllowedTools([]), []);
});

// ---------- toolArgs: the list is ENFORCED, not merely allowed ----------
// Live probe, claude 2.1.220 in claude-runner-commons, 2026-09-17: with
// `--dangerously-skip-permissions --allowedTools Read` the agent still ran Bash
// — --allowedTools adds permission ALLOW rules and skip-permissions bypasses
// every permission check, so the list restricted nothing since the runner
// shipped. `--tools Read` left the session with only Read. MCP tools are outside
// the built-in set, so --tools does not reach them: `--tools Read` still exposed
// mcp__ask-human__* and mcp__n8n__*, and `--disallowedTools mcp__ask-human`
// removed that server. These pin the flags that actually restrict.
const SERVERS = ["n8n", "ask-human"];
const flag = (args, name) => { const i = args.indexOf(name); return i < 0 ? undefined : args[i + 1]; };

test("toolArgs: a list goes to --tools, never to --allowedTools", () => {
  const args = toolArgs(["Read", "Grep"], SERVERS);
  assert.equal(flag(args, "--tools"), "Read,Grep");
  assert.equal(args.includes("--allowedTools"), false,
    "--allowedTools under skip-permissions restricts nothing");
});
test("toolArgs: every MCP server the list does not name is disallowed", () => {
  const args = toolArgs(["Read"], SERVERS);
  assert.deepEqual(flag(args, "--disallowedTools").split(",").sort(), ["mcp__ask-human", "mcp__n8n"]);
});
test("toolArgs: a named MCP server stays available, the rest are still denied", () => {
  const args = toolArgs(["Read", "mcp__ask-human"], SERVERS);
  assert.equal(flag(args, "--tools"), "Read", "mcp names never go to --tools");
  assert.equal(flag(args, "--disallowedTools"), "mcp__n8n");
});
test("toolArgs: an empty list disables every built-in AND every MCP server", () => {
  const args = toolArgs([], SERVERS);
  assert.equal(flag(args, "--tools"), "");
  assert.deepEqual(flag(args, "--disallowedTools").split(",").sort(), ["mcp__ask-human", "mcp__n8n"]);
});
test("toolArgs: null keeps the legacy defaults (no tool flags at all)", () => {
  assert.deepEqual(toolArgs(null, SERVERS), []);
  assert.deepEqual(toolArgs(undefined, SERVERS), []);
});
test("toolArgs: naming an MCP server this runner does not configure is refused", () => {
  assert.throws(() => toolArgs(["mcp__github"], SERVERS),
    (e) => e.statusCode === 422 && /github/.test(e.message));
});

// ---------- gemini-runner: a tool list it cannot enforce is refused ----------
// gemini-runner ran `--yolo` and never read allowedTools, so a narrowed request
// routed to the second vendor got every tool. Gemini's tool names differ from
// Claude's and its --allowed-tools flag is deprecated, so the one restriction it
// can honour is whole-session read-only (--approval-mode plan). That flag is
// unprobed here — the runner has no credentials — so it is operator opt-in via
// READONLY_ARGS, and without it a narrowed request fails closed.
const { geminiArgs, READ_ONLY_TOOLS } = gemini;
const YOLO = ["--yolo", "--output-format", "json"];
const PLAN = ["--approval-mode", "plan", "--output-format", "json"];

test("geminiArgs: no list keeps the legacy args", () => {
  assert.deepEqual(geminiArgs(null, { extraArgs: YOLO, readOnlyArgs: PLAN }), YOLO);
});
test("geminiArgs: a list naming a write/execute tool is refused, never run under --yolo", () => {
  // Built-in write/execute tools only. An mcp__ entry names a server this runner
  // does not load, so it grants nothing here (see the next test); the router
  // keeps execute-class MCP work on claude.
  for (const tools of [["Read", "Edit"], ["Bash"], ["Read", "mcp__n8n", "Write"]]) {
    assert.throws(() => geminiArgs(tools, { extraArgs: YOLO, readOnlyArgs: PLAN }),
      (e) => e.statusCode === 422, JSON.stringify(tools));
  }
});
test("geminiArgs: a read-only list with no READONLY_ARGS configured fails closed", () => {
  assert.throws(() => geminiArgs(["Read", "Grep"], { extraArgs: YOLO, readOnlyArgs: [] }),
    (e) => e.statusCode === 422 && /READONLY_ARGS/.test(e.message));
});
test("geminiArgs: a read-only list runs the read-only args, and never --yolo", () => {
  for (const tools of [["Read", "Grep"], []]) {
    const args = geminiArgs(tools, { extraArgs: YOLO, readOnlyArgs: PLAN });
    assert.deepEqual(args, PLAN);
    assert.equal(args.includes("--yolo"), false);
  }
});
test("geminiArgs: mcp__ entries do not make a read-only list unrunnable", () => {
  // The router's bound "advise" list includes mcp__ask-human. This runner has no
  // such server, so the entry grants nothing here, and refusing on it would 422
  // every read-only request the router sends.
  assert.deepEqual(geminiArgs(["Read", "Grep", "mcp__ask-human"], { extraArgs: YOLO, readOnlyArgs: PLAN }), PLAN);
});
test("geminiArgs: READ_ONLY_TOOLS agrees with policy.json risk.toolAccess", async () => {
  // Two lists of "harmless" tools is how one of them grows a write tool unseen.
  const { readFileSync } = await import("node:fs");
  const policy = JSON.parse(readFileSync(p("../../router/policy.json"), "utf8"));
  const A = policy.risk.access, T = policy.risk.toolAccess.tools;
  for (const tool of READ_ONLY_TOOLS)
    assert.ok(T[tool] && A[T[tool]] === 1, `${tool} is read-only in gemini-runner but maps to ${T[tool]}`);
});

// ---------- gemini-runner shares the safeCwd contract ----------
test("gemini safeCwd: same escape prevention", () => {
  assert.equal(gemini.safeCwd(), "/workspace");
  assert.throws(() => gemini.safeCwd("../escape"), /escapes the workspace/);
});

// ---------- fanout: the same boundary, on the endpoint that had none ----------
// M3. /run resolved its cwd through safeCwd from the start; fanout used
// path.join(WORKSPACE, repo), which resolves an escape and returns it happily.
// Two entry points into the same server, one guarded, one not — and the
// unguarded one is the worse half, because it clones the target and runs agents
// in worktrees under it.
//
// Asserted via the thrown message: reaching "escapes the workspace" proves the
// boundary check ran BEFORE anything touched the filesystem. A test that only
// checked "it threw" would pass on the vulnerable version too, which resolved
// the path fine and failed later inside git.
test("fanout: rejects a repo path that escapes the workspace", async () => {
  for (const repo of ["../..", "../../etc", "/etc", "sub/../../../root"]) {
    await assert.rejects(
      () => claude.fanout({ repo, tasks: [{ id: "t", prompt: "x" }] }),
      /escapes the workspace/,
      `repo ${JSON.stringify(repo)} must be refused at the boundary`,
    );
  }
});

test("fanout: an in-workspace repo passes the boundary and fails later", async () => {
  // "does not exist" means it got PAST the escape check — the boundary is not
  // rejecting everything, which would make the test above vacuous.
  await assert.rejects(
    () => claude.fanout({ repo: "some-project", tasks: [{ id: "t", prompt: "x" }] }),
    /does not exist in the workspace/,
  );
});

test("fanout: still requires tasks[] before touching the path", async () => {
  await assert.rejects(() => claude.fanout({ repo: "x" }), /tasks\[\] is required/);
});

// ---------- SCOPE_ROOT: the C2 fix, and M3's second half ----------
// safeCwd bounded to the WORKSPACE. A workspace boundary is not a scope
// boundary, so a token-holder could name any tenant's directory and the runner
// would work in it. gen_compose.py had been emitting SCOPE_ROOT per generated
// runner all along and nothing read it — the compose file advertised isolation
// that no code enforced.
//
// Loaded in a child process because SCOPE_ROOT is read at import time; setting
// it after the module is cached would test nothing.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

function underRoot(root, rel) {
  const script = `
    process.env.ALLOW_NO_AUTH = "1";
    process.env.SCOPE_ROOT = ${JSON.stringify(root)};
    const m = require(${JSON.stringify(p("../../claude-runner/server.js"))});
    try { console.log("OK:" + m.safeCwd(${JSON.stringify(rel)})); }
    catch (e) { console.log("THROW:" + e.message); }
  `;
  return execFileSync(process.execPath, ["-e", script], { encoding: "utf8" }).trim();
}

test("SCOPE_ROOT: another tenant's VALID workspace path is refused", () => {
  // The heart of it. This path is inside the workspace and may well exist —
  // pre-fix it was accepted, which is the whole finding.
  const out = underRoot("scopes/enterprise/consulting/tenant-a",
                        "scopes/enterprise/consulting/tenant-b");
  assert.match(out, /THROW:.*outside this runner's scope root/, out);
});

test("SCOPE_ROOT: the workspace root itself is refused to a scoped runner", () => {
  const out = underRoot("scopes/enterprise/consulting/tenant-a", ".");
  assert.match(out, /THROW:.*outside this runner's scope root/, out);
});

test("SCOPE_ROOT: a path INSIDE the root passes the boundary", () => {
  // Must fail on existence, not on the boundary — otherwise the guard could be
  // passing by refusing everything, and the test above would prove nothing.
  const out = underRoot("scopes/enterprise/consulting/tenant-a",
                        "scopes/enterprise/consulting/tenant-a/ingest");
  assert.match(out, /THROW:.*does not exist in the workspace/, out);
});

test("SCOPE_ROOT: escaping the workspace still reports the workspace boundary", () => {
  const out = underRoot("scopes/enterprise/consulting/tenant-a", "../../etc/passwd");
  assert.match(out, /THROW:.*escapes the workspace/, out);
});

test("SCOPE_ROOT unset: full workspace, unchanged commons behaviour", () => {
  const out = underRoot("", "scopes");
  assert.ok(/THROW:.*does not exist/.test(out) || /^OK:/.test(out),
            `commons runner must not gain a scope error: ${out}`);
  assert.doesNotMatch(out, /scope root/, out);
});

// ---------- In-runner PII enforcement: the direct-call bypass ----------
// The router refused raw PII for a pii:block scope and was the ONLY place it
// was enforced, so anything reaching a runner directly skipped it entirely —
// which is the whole reason C2 says move the PEP inward.
//
// Policy is read from the environment at import, so each case runs in a child.
function withPolicy(policy, prompt, runner = "claude-runner") {
  const script = `
    process.env.ALLOW_NO_AUTH = "1";
    process.env.PII_POLICY = ${JSON.stringify(policy)};
    const m = require(${JSON.stringify(p(`../../${runner}/server.js`))});
    try { const h = m.enforcePii(${JSON.stringify(prompt)});
          console.log("OK:" + JSON.stringify(h)); }
    catch (e) { console.log("THROW:" + e.statusCode + ":" + e.message); }
  `;
  return execFileSync(process.execPath, ["-e", script], { encoding: "utf8" }).trim();
}

const RAW_SSN = "please review the file for 123-45-6789 and summarise";

test("PII block: a raw SSN is refused, with 422 not 500", () => {
  const out = withPolicy("block", RAW_SSN);
  assert.match(out, /^THROW:422:/, out);
  assert.match(out, /ssn/, out);
  // The refusal must say what to do instead, or the caller retries verbatim.
  assert.match(out, /[Tt]okenize/, out);
});

test("PII block: both runners refuse identically", () => {
  const a = withPolicy("block", RAW_SSN, "claude-runner");
  const b = withPolicy("block", RAW_SSN, "gemini-runner");
  assert.match(b, /^THROW:422:/, b);
  assert.equal(a, b, "runners must not disagree about what PII is");
});

test("PII block: a clean prompt is untouched", () => {
  // Else the guard passes by refusing everything, and the test above is empty.
  assert.match(withPolicy("block", "summarise the architecture doc"), /^OK:null/);
});

test("PII warn (the default): detects and proceeds", () => {
  const out = withPolicy("warn", RAW_SSN);
  assert.match(out, /^OK:/, out);
  assert.match(out, /ssn/, out);
});

test("PII off: skips entirely", () => {
  assert.match(withPolicy("off", RAW_SSN), /^OK:null/);
});

test("PII policy comes from the ENV, and a payload cannot lower it", () => {
  // The bypass this guards against is a direct caller, so a caller-supplied
  // policy or allowPii flag would hand the bypass straight back. enforcePii
  // takes only the prompt — there is no argument through which to weaken it.
  assert.equal(withPolicy("block", RAW_SSN).startsWith("THROW:422:"), true);
  const src = readFileSync(p("../../claude-runner/server.js"), "utf8");
  assert.doesNotMatch(src, /enforcePii\([^)]*allowPii/,
                      "enforcePii must not accept a caller-supplied override");
});

test("PII: email and card are caught too, not just SSN", () => {
  assert.match(withPolicy("block", "mail me at a.b@example.com"), /^THROW:422:.*email/);
  assert.match(withPolicy("block", "card 4111 1111 1111 1111"), /^THROW:422:.*card/);
});
