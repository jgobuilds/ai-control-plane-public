// HTTP wrapper around `claude -p`. No dependencies — Node's http module.
//
// POST /run     single task; optional fresh-context verifier. See fields below.
// POST /fanout  parallel worktree-per-agent orchestration (AG-2). See below.
// GET  /health  -> "ok"
//
// Loop discipline (the ladder): agent + verifier, every loop bounded by a budget
// + stop condition (maxTurns), review with FRESH context (a separate process).
// Fan-out isolation: each agent works in its own git worktree so parallel edits
// never collide; the runner NEVER merges or pushes — merge stays a human act.
const http = require("http");
const crypto = require("crypto");
const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");

const TOKEN = process.env.RUNNER_TOKEN || "";
const PORT = 8080;

// Fail closed: refuse to boot without auth unless explicitly opted out for dev.
if (!TOKEN && process.env.ALLOW_NO_AUTH !== "1") {
  console.error("claude-runner: RUNNER_TOKEN must be set (or ALLOW_NO_AUTH=1 for local dev only). Refusing to start.");
  process.exit(1);
}
function tokenOk(provided, expected) {
  if (!expected) return process.env.ALLOW_NO_AUTH === "1";
  const a = Buffer.from(String(provided || ""));
  const b = Buffer.from(expected);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
const DEFAULT_MAX_TURNS = parseInt(process.env.DEFAULT_MAX_TURNS || "40", 10);
const FANOUT_CONCURRENCY = parseInt(process.env.FANOUT_CONCURRENCY || "4", 10);
const WORKSPACE = "/workspace";
const WORKTREES = "/worktrees"; // ephemeral, container-local; branches/patches persist

function exec(cmd, args, opts = {}) {
  return new Promise((resolve, reject) => {
    execFile(cmd, args, { maxBuffer: 20 * 1024 * 1024, ...opts }, (err, stdout, stderr) => {
      if (err) { err.stdout = stdout; err.stderr = stderr; return reject(err); }
      resolve({ stdout, stderr });
    });
  });
}
const git = (dir, args, opts) => exec("git", ["-C", dir, ...args], opts);
const tail = (s, n = 4000) => { s = String(s || ""); return s.length > n ? s.slice(-n) : s; };

// Scope-aware working dir: the router passes a relative cwd (a scope node's
// directory). Resolve it and refuse anything that escapes the workspace.
// SCOPE_ROOT is the C2 fix: enforcement in the RUNNER, not only in the router.
// gen_compose.py has emitted this per generated runner since the isolation work
// began and NOTHING READ IT — the compose file advertised per-tenant isolation
// that no code enforced. An env var nobody reads is a comment.
//
// Empty means the whole workspace: the shared "commons" runner for
// low-sensitivity scopes. That is a legitimate configuration, so it is logged at
// boot rather than assumed — an unscoped runner should be a visible choice.
const SCOPE_ROOT = String(process.env.SCOPE_ROOT || "").trim().replace(/^\/+|\/+$/g, "");
const ROOT_DIR = SCOPE_ROOT ? path.resolve(WORKSPACE, SCOPE_ROOT) : WORKSPACE;

// In-runner PII enforcement — the second half of moving the PEP inward (C2).
// The router already refuses raw PII for a `pii:block` scope, and that is the
// ONLY place it was enforced, so anything reaching a runner directly bypassed
// it entirely. The router is the policy decision point; this is the enforcement
// point, and an enforcement point that only works when the caller went through
// the front door is not one.
//
// POLICY COMES FROM THE ENVIRONMENT, NEVER THE PAYLOAD. A direct caller is
// exactly who this guards against, so letting the request declare its own
// policy — or pass `allowPii` — would hand the bypass back. gen_compose.py sets
// this per silo runner from the scope's own controls.
//
//   block  refuse the request
//   warn   scan and log, proceed (default: visibility everywhere, breaks nothing)
//   off    skip entirely
//
// DETECTION ONLY, never tokenization: the scrubber's vault is never mounted
// into a runner, and that constraint is load-bearing. A runner that could
// de-tokenize would make the whole PII model decorative.
const { piiScan } = require("./pii-patterns.js");
const PII_POLICY = String(process.env.PII_POLICY || "warn").trim().toLowerCase();

function enforcePii(prompt) {
  if (PII_POLICY === "off") return null;
  const hits = piiScan(prompt);
  if (!hits.length) return null;
  const types = hits.map((h) => h.type).join(", ");
  if (PII_POLICY === "block") {
    const e = new Error(
      `raw PII (${types}) in prompt and this runner enforces PII_POLICY=block. ` +
      `Tokenize via the scrubber before dispatch. The runner cannot de-tokenize ` +
      `and must not: the vault is never mounted here.`);
    e.statusCode = 422;   // the request is unprocessable as sent, not a server fault
    throw e;
  }
  console.warn(`PII WARN: ${types} in prompt (policy=${PII_POLICY})`);
  return hits;
}

function safeCwd(rel) {
  // Paths arrive WORKSPACE-relative (the router speaks scope-node paths), so
  // resolve against the workspace and then assert containment in this runner's
  // root. Resolving against ROOT_DIR instead would silently re-root
  // "scopes/a/b" under the root and land somewhere that does not exist.
  // Empty means "this runner's base", returned WITHOUT an existence check —
  // exactly as before. Falling through to fs.existsSync changed the contract: it
  // made safeCwd() throw wherever the base does not exist on disk, which is
  // benign in the image and false everywhere else. CI caught it the first time
  // it ran these tests.
  if (!rel) return ROOT_DIR;
  const p = path.resolve(WORKSPACE, rel);
  if (p !== WORKSPACE && !p.startsWith(WORKSPACE + path.sep))
    throw new Error(`cwd "${rel}" escapes the workspace`);
  // Threat-model M3, second half: a token-holder could name ANY scope that
  // exists, because a workspace boundary is not a scope boundary. With a root
  // set, this runner can only work inside it — structurally, not by policy.
  if (p !== ROOT_DIR && !p.startsWith(ROOT_DIR + path.sep))
  {
    // 403, not 500: this is a refusal, and a caller must be able to tell that
    // from a fault. Retrying a scope violation can never succeed.
    const e = new Error(`cwd "${rel}" is outside this runner's scope root "${SCOPE_ROOT}"`);
    e.statusCode = 403;
    throw e;
  }
  if (!fs.existsSync(p)) throw new Error(`cwd "${rel}" does not exist in the workspace`);
  return p;
}

// Per-scope tool narrowing (excessive-agency). The router passes a list of tool
// names; this function validates it and toolArgs() turns it into the flags that
// actually RESTRICT. Returns a cleaned array, or null for "no list".
//
// THIS LIST USED TO RESTRICT NOTHING. It went to --allowedTools, which adds
// permission ALLOW rules — and --dangerously-skip-permissions bypasses every
// permission check, so unlisted tools were never denied. Probed live
// 2026-09-17 (claude 2.1.220): `--allowedTools Read` still ran Bash. Anything
// that only makes sense as a permission rule is therefore refused rather than
// passed along to do nothing:
//   - specifiers like "Bash(git log:*)": --tools takes built-in NAMES only, so
//     the choice is widen-to-all-of-Bash or restrict nothing. Neither is honest.
//   - tool-level MCP names like "mcp__srv__tool": MCP is narrowed per SERVER
//     (see toolArgs), so name the server.
// An empty list stays an empty list: it means no tools. It used to collapse to
// null — pass no flag — which made the most restrictive list the least.
const unenforceable = (msg) => Object.assign(new Error(msg), { statusCode: 422 });
function sanitizeAllowedTools(list) {
  if (list == null) return null;
  if (!Array.isArray(list)) throw unenforceable("allowedTools must be an array of tool-name strings");
  const clean = [];
  for (const t of list) {
    if (typeof t !== "string" || !t.trim()) throw unenforceable("allowedTools entries must be non-empty strings");
    const tok = t.trim();
    if (tok.startsWith("-")) throw unenforceable(`allowedTools entry "${tok}" may not start with '-'`);
    // execFile passes args as an array (no shell), so this is belt-and-braces
    // plus anti-flag-injection. Reject ; & | $ ` < > {} \ ' " newline etc.
    if (!/^[A-Za-z0-9_.:()*/\s-]+$/.test(tok))
      throw unenforceable(`allowedTools entry "${tok}" contains disallowed characters`);
    if (tok.includes("("))
      throw unenforceable(`allowedTools entry "${tok}" is a permission specifier and cannot be enforced under --dangerously-skip-permissions; name the whole tool or leave it out`);
    const mcp = /^mcp__(.+?)__(.+)$/.exec(tok);
    if (mcp)
      throw unenforceable(`allowedTools entry "${tok}" names one MCP tool; MCP is narrowed per server, so name "mcp__${mcp[1]}"`);
    clean.push(tok);
  }
  return clean;
}

// MCP servers this runner loads. Read per call rather than cached so the list a
// request is narrowed against is the config the CLI is about to load.
const MCP_CONFIG = process.env.MCP_CONFIG || "/app/.mcp.json";
function mcpServerNames(file = MCP_CONFIG) {
  try { return Object.keys(JSON.parse(fs.readFileSync(file, "utf8")).mcpServers || {}); }
  catch { return []; }
}

// The flags that make the list binding. Built-in names go to --tools, which
// sets what EXISTS in the session (probed: `--tools Read` leaves only Read).
// --tools does not reach MCP tools — probed: `--tools Read` still exposed
// mcp__ask-human__* and mcp__n8n__* — so every configured server the list does
// not name goes to --disallowedTools, which does remove a server's tools even
// under skip-permissions (probed: `--disallowedTools mcp__ask-human`).
// null means no list: no flags, the legacy full toolset.
function toolArgs(list, servers = mcpServerNames()) {
  const tools = sanitizeAllowedTools(list);
  if (tools === null) return [];
  const builtins = tools.filter((t) => !t.startsWith("mcp__"));
  const named = new Set(tools.filter((t) => t.startsWith("mcp__")).map((t) => t.slice("mcp__".length)));
  const unknown = [...named].filter((s) => !servers.includes(s));
  if (unknown.length)
    throw unenforceable(`allowedTools names MCP server(s) this runner does not configure: ${unknown.join(", ")}`);
  const args = ["--tools", builtins.join(",")];
  const denied = servers.filter((s) => !named.has(s)).map((s) => `mcp__${s}`);
  if (denied.length) args.push("--disallowedTools", denied.join(","));
  return args;
}

// Normalize whatever the CLI reports into one stable shape.
//
// IMPORTANT: `notionalCostUsd` is what the work WOULD have cost at metered API
// rates. These runners authenticate on a SUBSCRIPTION, so it is NOT money
// spent — it is the only common yardstick across tiers and providers, which is
// what makes tier decisions comparable. Never render it as "spend". See AIOPS.md.
function normalizeUsage(parsed, wallMs) {
  const u = (parsed && parsed.usage) || {};
  const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : null);
  const inTok = num(u.input_tokens);
  const outTok = num(u.output_tokens);
  const cacheRead = num(u.cache_read_input_tokens);
  const cacheWrite = num(u.cache_creation_input_tokens);
  return {
    notionalCostUsd: num(parsed && parsed.total_cost_usd),
    inputTokens: inTok,
    outputTokens: outTok,
    cacheReadTokens: cacheRead,
    cacheCreationTokens: cacheWrite,
    // Total input actually processed, cached or not — the number that matters
    // for context-window pressure (input_tokens excludes the cached remainder).
    totalInputTokens: [inTok, cacheRead, cacheWrite].some((n) => n !== null)
      ? (inTok || 0) + (cacheRead || 0) + (cacheWrite || 0)
      : null,
    turns: num(parsed && parsed.num_turns),
    // CLI-reported duration when present; wall time is the fallback and is
    // always recorded so latency is never simply absent.
    durationMs: num(parsed && parsed.duration_ms) ?? wallMs,
    wallMs,
    isError: parsed && typeof parsed.is_error === "boolean" ? parsed.is_error : null,
  };
}

function claudeJson({ prompt, model, maxTurns, cwd = WORKSPACE, allowedTools }) {
  return new Promise((resolve, reject) => {
    const args = [
      "-p", prompt,
      "--output-format", "json",
      "--max-turns", String(maxTurns || DEFAULT_MAX_TURNS),
      "--mcp-config", MCP_CONFIG,
      // Safe ONLY because egress is firewalled and mounts are limited. Don't copy.
      "--dangerously-skip-permissions",
    ];
    // Tool narrowing: --tools + --disallowedTools, NOT --allowedTools (which
    // restricts nothing under skip-permissions). See toolArgs.
    let narrowing;
    try { narrowing = toolArgs(allowedTools); } catch (e) { return reject(e); }
    args.push(...narrowing);
    if (model) args.push("--model", model);
    const startedAt = Date.now();
    execFile(
      "claude", args,
      { cwd, maxBuffer: 20 * 1024 * 1024, timeout: 15 * 60 * 1000 },
      (err, stdout, stderr) => {
        if (err) {
          // Report BOTH. Preferring stderr alone let a harmless CLI warning
          // ("no stdin data received in 3s") stand in for the actual failure
          // (--max-turns exhausted), so the lane's error message pointed at
          // nothing and cost a debugging cycle. stderr is context; err.message
          // is the cause, and the cause must never be dropped.
          const why = [err.message, stderr?.trim()].filter(Boolean).join(" | ");
          return reject(new Error(why || "claude CLI failed with no output"));
        }
        const wallMs = Date.now() - startedAt;
        let parsed;
        try { parsed = JSON.parse(stdout); } catch { parsed = { result: stdout }; }
        // Attach normalized economics. The CLI already returns these and we were
        // discarding them — see AIOPS.md. `usage` is a stable shape the router
        // can write to the ledger without knowing CLI internals.
        parsed.usage = normalizeUsage(parsed, wallMs);
        resolve(parsed);
      }
    );
  });
}

// Fresh-context verifier: a new process seeing only goal + output, not the
// executor's reasoning — so it can't inherit the agent's bias.
async function verify({ goal, outputText, reviewModel, maxTurns }) {
  const prompt = [
    "You are a strict, independent reviewer with no prior context.",
    "Judge ONLY whether the work below satisfies the stated goal. Do not fix it.",
    'Respond with a single JSON object and nothing else:',
    '{ "pass": true|false, "reasons": ["..."] }',
    "",
    `GOAL:\n${goal || "(no explicit goal — judge for correctness and completeness)"}`,
    "",
    `WORK TO REVIEW:\n${outputText || "(empty)"}`,
  ].join("\n");
  const res = await claudeJson({ prompt, model: reviewModel, maxTurns: maxTurns || 10 });
  const text = typeof res.result === "string" ? res.result : JSON.stringify(res.result);
  const m = text && text.match(/\{[\s\S]*\}/);
  if (m) { try { return JSON.parse(m[0]); } catch { /* fall through */ } }
  return { pass: null, reasons: ["verifier did not return parseable JSON"], raw: text };
}

const textOf = (r) => (r && typeof r.result === "object" ? (r.result.result ?? "") : (r?.result ?? ""));

// --- one agent, isolated in its own worktree ---
async function runTaskInWorktree(repoDir, base, task, clean) {
  const id = String(task.id || "task").replace(/[^a-zA-Z0-9._-]/g, "-");
  const branch = `agent/${id}`;
  const wt = path.join(WORKTREES, id);
  const fanoutDir = path.join(WORKSPACE, ".fanout");
  const out = { id, branch, status: "ok" };

  if (clean) {
    try { await git(repoDir, ["worktree", "remove", "--force", wt]); } catch { /* none */ }
    try { await git(repoDir, ["branch", "-D", branch]); } catch { /* none */ }
  }
  try {
    fs.mkdirSync(fanoutDir, { recursive: true });
    await git(repoDir, ["worktree", "add", "-b", branch, wt, base]);
  } catch (e) {
    return { ...out, status: "error",
      error: `worktree add failed (id in use? pass "clean":true or remove it): ${tail(e.stderr || e.message, 500)}` };
  }
  try {
    const res = await claudeJson({ prompt: task.prompt, model: task.model, maxTurns: task.maxTurns, cwd: wt, allowedTools: task.allowedTools });
    out.agentSummary = tail(textOf(res), 2000);

    await git(wt, ["add", "-A"]);
    const { stdout: st } = await git(wt, ["status", "--porcelain"]);
    if (!st.trim()) {
      out.status = "no-change";
    } else {
      await git(wt, ["-c", "user.email=agent@runner", "-c", `user.name=agent-${id}`,
        "commit", "-m", `agent: ${task.id || id}`]);
      out.diffStat = (await git(wt, ["diff", "--stat", base, "HEAD"])).stdout.trim();
      const patch = (await git(wt, ["diff", base, "HEAD"])).stdout;
      out.patchFile = path.join(fanoutDir, `${id}.patch`);
      fs.writeFileSync(out.patchFile, patch);
    }

    if (task.verify) {
      try {
        const v = await exec("bash", ["-lc", task.verify], { cwd: wt, timeout: 10 * 60 * 1000 });
        out.verify = { ran: true, passed: true, exitCode: 0, output: tail(v.stdout + v.stderr) };
      } catch (e) {
        out.verify = { ran: true, passed: false, exitCode: e.code ?? 1,
          output: tail((e.stdout || "") + (e.stderr || e.message)) };
        if (out.status !== "no-change") out.status = "failed";
      }
    }
  } catch (e) {
    out.status = "error";
    out.error = tail(e.stderr || e.message, 1000);
  }
  return out;
}

// bounded-concurrency pool (the "~10 agents" don't all run at once)
async function pool(items, size, fn) {
  const results = new Array(items.length);
  let i = 0;
  const worker = async () => { while (i < items.length) { const idx = i++; results[idx] = await fn(items[idx]); } };
  await Promise.all(Array.from({ length: Math.min(size, items.length) }, worker));
  return results;
}

async function fanout({ repo = ".", base = "main", tasks = [], clean = false }) {
  if (!Array.isArray(tasks) || !tasks.length) throw new Error("tasks[] is required");
  // safeCwd, NOT path.join (threat-model M3). `path.join(WORKSPACE, "../..")`
  // happily resolves outside the workspace and returns it — so this endpoint
  // accepted an escape that /run had always refused, in the same file, through a
  // helper written for exactly this. The fan-out path is the worse one to leave
  // open: it clones the target and runs agents in worktrees under it.
  const repoDir = safeCwd(repo);
  // Refuse an unenforceable tool list BEFORE any worktree exists. Found at
  // dispatch it would surface as one task's error after its branch was created,
  // and the other agents would already be running.
  for (const t of tasks) toolArgs(t && t.allowedTools);
  await git(repoDir, ["rev-parse", "--is-inside-work-tree"]); // must be a git repo
  const results = await pool(tasks, FANOUT_CONCURRENCY, (t) => runTaskInWorktree(repoDir, base, t, clean));
  const changed = results.filter((r) => r.patchFile).length;
  const passed = results.filter((r) => r.verify?.passed).length;
  const verified = results.filter((r) => r.verify?.ran).length;
  return {
    base,
    results,
    review:
      `${tasks.length} agents ran · ${changed} produced changes · ${passed}/${verified} passed self-verify. ` +
      `Nothing was merged or pushed. Review branches agent/<id> (or patches in ${WORKSPACE}/.fanout), ` +
      `then merge the ones you want: \`git merge agent/<id>\`.`,
  };
}

// ask-human answers waiting to be collected, keyed by ticket. Bounded by TTL so
// an unanswered question cannot pin memory: nothing here outlives the poll that
// would have read it.
const askMailbox = new Map();
const ASK_TTL_MS = 30 * 60 * 1000;
function pruneMailbox() {
  const cutoff = Date.now() - ASK_TTL_MS;
  for (const [k, v] of askMailbox) if ((v.at || 0) < cutoff) askMailbox.delete(k);
}

const server = http.createServer((req, res) => {
  const send = (code, obj) =>
    res.writeHead(code, { "content-type": "application/json" }).end(JSON.stringify(obj));

  if (req.method === "GET" && req.url === "/health") return res.writeHead(200).end("ok");

  // ---- ask-human answer mailbox ---------------------------------------------
  // The agent's ask_human MCP tool blocks inside a tool call while a human is
  // paged, and the answer has to reach THIS container. The runner's egress
  // allowlist deliberately does not include the router, so the answer is not
  // fetched — it is PUSHED IN by the router, on the port already open for
  // /run. The MCP tool then reads it over loopback, which the firewall allows.
  //
  // In memory on purpose: if this process restarts, the `claude` run it was
  // serving died with it, so there would be nobody left for a persisted answer
  // to return to.
  if (req.url.startsWith("/ask/status")) {
    if (!tokenOk(req.headers["x-runner-token"], TOKEN)) return res.writeHead(401).end("unauthorized");
    const ticket = new URL(req.url, "http://x").searchParams.get("ticket") || "";
    const hit = askMailbox.get(ticket);
    if (!hit) return send(200, { state: "pending", ticket });
    askMailbox.delete(ticket);        // deliver once — the poller is done with it
    return send(200, { state: "done", ticket, ...hit });
  }
  if (req.method === "POST" && req.url === "/ask/deliver") {
    if (!tokenOk(req.headers["x-runner-token"], TOKEN)) return res.writeHead(401).end("unauthorized");
    let b = "";
    req.on("data", (c) => (b += c));
    req.on("end", () => {
      let a;
      try { a = JSON.parse(b || "{}"); } catch { return send(400, { error: "bad json" }); }
      if (!a.ticket) return send(400, { error: "ticket is required" });
      askMailbox.set(a.ticket, {
        answered: a.answered === true,
        answer: a.answer || "",
        timedOut: a.timedOut === true,
        note: a.note || "",
        at: Date.now(),
      });
      pruneMailbox();
      return send(200, { stored: a.ticket });
    });
    return;
  }

  if (req.method !== "POST" || !["/run", "/fanout"].includes(req.url))
    return res.writeHead(404).end("not found");
  if (!tokenOk(req.headers["x-runner-token"], TOKEN)) return res.writeHead(401).end("unauthorized");

  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", async () => {
    try {
      const input = JSON.parse(body || "{}");
      if (req.url === "/fanout") {
        // Every task's prompt, not just the first. A fan-out is N prompts and
        // checking one of them is theatre.
        for (const t of (input.tasks || [])) enforcePii(t && t.prompt);
        return send(200, await fanout(input));
      }

      // /run
      const { prompt, model, verify: doVerify, goal, reviewModel, maxTurns, cwd, allowedTools } = input;
      if (!prompt) return send(400, { error: "prompt is required" });
      enforcePii(prompt);
      const result = await claudeJson({ prompt, model, maxTurns, cwd: safeCwd(cwd), allowedTools });
      let verdict = null;
      if (doVerify) {
        const outputText = typeof result.result === "string" ? result.result : JSON.stringify(result.result);
        verdict = await verify({ goal, outputText, reviewModel, maxTurns });
      }
      // usage is part of the runner's response CONTRACT, not buried in the
      // provider payload — the router should not have to know CLI internals.
      send(200, { result, verdict, usage: result && result.usage ? result.usage : null });
    } catch (e) {
      // Honour a status the guard chose. Everything returned 500, so a caller
      // could not tell "you are not allowed to do that" from "the runner broke"
      // — and only the second is worth retrying. A client that retries a policy
      // refusal turns one refusal into a loop.
      send(e.statusCode || 500, { error: String(e.message || e) });
    }
  });
});

// Start the HTTP server ONLY when run directly; export pure helpers for tests.
if (require.main === module) {
  server.listen(PORT, () => {
    console.log(`claude-runner listening on :${PORT}`);
    // Say which boundary is in force. "Isolated" and "not isolated" must not
    // look identical in the logs — that is how a commons runner gets mistaken
    // for a tenant one during an incident.
    console.log(SCOPE_ROOT
      ? `scope root ENFORCED: ${ROOT_DIR} (paths outside it are refused)`
      : `scope root NOT SET: full workspace access — commons runner`);
  });
}

module.exports = {
  tokenOk,
  safeCwd,
  sanitizeAllowedTools,
  toolArgs,
  fanout,
  enforcePii,
};
