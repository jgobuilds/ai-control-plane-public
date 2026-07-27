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
function safeCwd(rel) {
  if (!rel) return WORKSPACE;
  const p = path.resolve(WORKSPACE, rel);
  if (p !== WORKSPACE && !p.startsWith(WORKSPACE + path.sep))
    throw new Error(`cwd "${rel}" escapes the workspace`);
  if (!fs.existsSync(p)) throw new Error(`cwd "${rel}" does not exist in the workspace`);
  return p;
}

// Per-scope tool allowlisting (excessive-agency / defence-in-depth). The router
// passes the resolved scope's controls.allowedTools; we validate it's an array of
// simple tool-name tokens and reject anything with shell metacharacters or a
// leading '-' (flag injection). Returns a cleaned array or null. Unlisted tools
// are effectively denied by Claude Code once --allowedTools is present.
function sanitizeAllowedTools(list) {
  if (list == null) return null;
  if (!Array.isArray(list)) throw new Error("allowedTools must be an array of tool-name strings");
  const clean = [];
  for (const t of list) {
    if (typeof t !== "string" || !t.trim()) throw new Error("allowedTools entries must be non-empty strings");
    const tok = t.trim();
    if (tok.startsWith("-")) throw new Error(`allowedTools entry "${tok}" may not start with '-'`);
    // Allow tool names + Claude Code specifiers, e.g. Read, Bash, mcp__srv__tool,
    // "Bash(git log:*)". Reject shell metacharacters ; & | $ ` < > {} \ ' " newline.
    if (!/^[A-Za-z0-9_.:()*/\s-]+$/.test(tok))
      throw new Error(`allowedTools entry "${tok}" contains disallowed characters`);
    clean.push(tok);
  }
  return clean.length ? clean : null;
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
      "--mcp-config", "/app/.mcp.json",
      // Safe ONLY because egress is firewalled and mounts are limited. Don't copy.
      "--dangerously-skip-permissions",
    ];
    // Claude Code flag name is --allowedTools (comma-separated). If a build of the
    // CLI expects --allowed-tools instead, adjust here — flagged verify-on-live in
    // GUARDRAILS.md. execFile passes args as an array (no shell), so tokens are not
    // shell-interpreted; sanitizeAllowedTools is belt-and-suspenders + anti-flag-injection.
    const tools = sanitizeAllowedTools(allowedTools);
    if (tools) args.push("--allowedTools", tools.join(","));
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
  const repoDir = path.join(WORKSPACE, repo);
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
      if (req.url === "/fanout") return send(200, await fanout(input));

      // /run
      const { prompt, model, verify: doVerify, goal, reviewModel, maxTurns, cwd, allowedTools } = input;
      if (!prompt) return send(400, { error: "prompt is required" });
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
      send(500, { error: String(e.message || e) });
    }
  });
});

// Start the HTTP server ONLY when run directly; export pure helpers for tests.
if (require.main === module) {
  server.listen(PORT, () => console.log(`claude-runner listening on :${PORT}`));
}

module.exports = {
  tokenOk,
  safeCwd,
  sanitizeAllowedTools,
};
