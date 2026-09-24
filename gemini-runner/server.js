// HTTP wrapper around a headless agent CLI (Gemini by default; provider-flagged
// via AGENT_BIN so it also fits Antigravity or any `-p`-style CLI).
// Mirrors claude-runner's contract so the router can treat runners uniformly.
//
// POST /run { "prompt": "...", "model": "optional" } -> { result, provider, raw }
// GET  /health -> "ok"
const http = require("http");
const crypto = require("crypto");
const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");

const TOKEN = process.env.RUNNER_TOKEN || "";
const PORT = 8080;
if (!TOKEN && process.env.ALLOW_NO_AUTH !== "1") {
  console.error("gemini-runner: RUNNER_TOKEN must be set (or ALLOW_NO_AUTH=1 for local dev only). Refusing to start.");
  process.exit(1);
}
function tokenOk(provided, expected) {
  if (!expected) return process.env.ALLOW_NO_AUTH === "1";
  const a = Buffer.from(String(provided || ""));
  const b = Buffer.from(expected);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
const BIN = process.env.AGENT_BIN || "gemini";
const PROVIDER = process.env.PROVIDER || "gemini";
const PROMPT_FLAG = process.env.PROMPT_FLAG || "-p";
const MODEL_FLAG = process.env.MODEL_FLAG || "-m";
// Flags that make the CLI non-interactive + machine-readable. Gemini uses
// --yolo (auto-approve tools) and --output-format json. Adjust per CLI.
const splitArgs = (v) => String(v || "").split(/\s+/).filter(Boolean);
const EXTRA_ARGS = splitArgs(process.env.EXTRA_ARGS || "--yolo --output-format json");
// Args for a session narrowed to read-only tools, e.g.
// "--approval-mode plan --output-format json". Deliberately NO default: plan mode
// has not been probed headless on this runner (it has no credentials), and a
// default that turned out not to restrict would be the --allowedTools failure
// again. Unset means a narrowed request is refused.
const READONLY_ARGS = splitArgs(process.env.READONLY_ARGS);

// The router's tool list is in Claude tool names, and this CLI's --allowed-tools
// is deprecated, so the one restriction this runner can honour is "read-only for
// the whole session". It must agree with policy.json risk.toolAccess (a unit
// test holds the two together).
const READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "LS", "NotebookRead", "TodoWrite"];

// This runner used to ignore allowedTools and run --yolo, so a request the
// router had narrowed got every tool once it reached the second vendor. Now:
// no list keeps the legacy args; a list containing only read-only tools runs
// READONLY_ARGS; any other list is refused. Never --yolo under a list.
function geminiArgs(allowedTools, { extraArgs = EXTRA_ARGS, readOnlyArgs = READONLY_ARGS } = {}) {
  if (allowedTools == null) return extraArgs;
  const refuse = (msg) => Object.assign(new Error(msg), { statusCode: 422 });
  if (!Array.isArray(allowedTools)) throw refuse("allowedTools must be an array of tool-name strings");
  // mcp__<server> entries are permissions to use a server. This runner loads no
  // MCP servers under those names, so dropping them only narrows further. Keeping them
  // would refuse every bound "advise" request, which the router sends with
  // mcp__ask-human included.
  const beyond = allowedTools
    .map((t) => String(t).trim())
    .filter((t) => !t.startsWith("mcp__") && !READ_ONLY_TOOLS.includes(t));
  if (beyond.length)
    throw refuse(`${PROVIDER}-runner cannot restrict a session to [${allowedTools.join(", ")}]; it can only run read-only. Route this to claude.`);
  if (!readOnlyArgs.length)
    throw refuse(`${PROVIDER}-runner has no READONLY_ARGS configured, so it cannot enforce a read-only tool list. Route this to claude.`);
  return readOnlyArgs;
}

const WORKSPACE = "/workspace";
// Kept byte-for-byte equivalent to claude-runner's copy on purpose. Two runners
// with different boundary rules is how one of them ends up being the way in, and
// a divergence here would not fail any test that only exercised the other.
const SCOPE_ROOT = String(process.env.SCOPE_ROOT || "").trim().replace(/^\/+|\/+$/g, "");
const ROOT_DIR = SCOPE_ROOT ? path.resolve(WORKSPACE, SCOPE_ROOT) : WORKSPACE;
function safeCwd(rel) {
  // Empty means "this runner's base", returned WITHOUT an existence check —
  // exactly as before. Falling through to fs.existsSync changed the contract: it
  // made safeCwd() throw wherever the base does not exist on disk, which is
  // benign in the image and false everywhere else. CI caught it the first time
  // it ran these tests.
  if (!rel) return ROOT_DIR;
  const p = path.resolve(WORKSPACE, rel);
  if (p !== WORKSPACE && !p.startsWith(WORKSPACE + path.sep))
    throw new Error(`cwd "${rel}" escapes the workspace`);
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

// In-runner PII enforcement — identical to claude-runner's by design. Policy
// comes from the environment, never the payload: a direct caller is exactly who
// this guards against, so a request that could declare its own policy would
// hand the bypass straight back. Detection only; the vault is never mounted here.
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
    e.statusCode = 422;
    throw e;
  }
  console.warn(`PII WARN: ${types} in prompt (policy=${PII_POLICY})`);
  return hits;
}

function runAgent({ prompt, model, cwd, allowedTools }) {
  return new Promise((resolve, reject) => {
    let mode;
    try { mode = geminiArgs(allowedTools); } catch (e) { return reject(e); }
    const args = [PROMPT_FLAG, prompt, ...mode];
    if (model) args.push(MODEL_FLAG, model);

    execFile(
      BIN,
      args,
      { cwd: safeCwd(cwd), maxBuffer: 20 * 1024 * 1024, timeout: 15 * 60 * 1000 },
      (err, stdout, stderr) => {
        if (err) return reject(new Error(stderr?.trim() || err.message));
        // Normalize to { result: <text> } so callers read `.result` regardless
        // of provider. Gemini's JSON puts text in `response`; tolerate variants.
        let text = stdout, raw = null;
        try {
          raw = JSON.parse(stdout);
          text = raw.response ?? raw.result ?? raw.text ?? stdout;
        } catch { /* plain-text output */ }
        resolve({ result: text, provider: PROVIDER, raw });
      }
    );
  });
}

const server = http.createServer((req, res) => {
  if (req.method === "GET" && req.url === "/health") return res.writeHead(200).end("ok");
  if (req.method !== "POST" || req.url !== "/run") return res.writeHead(404).end("not found");
  if (!tokenOk(req.headers["x-runner-token"], TOKEN)) return res.writeHead(401).end("unauthorized");

  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", async () => {
    const send = (code, obj) =>
      res.writeHead(code, { "content-type": "application/json" }).end(JSON.stringify(obj));
    try {
      const { prompt, model, cwd, allowedTools } = JSON.parse(body || "{}");
      if (!prompt) return send(400, { error: "prompt is required" });
      enforcePii(prompt);
      send(200, await runAgent({ prompt, model, cwd, allowedTools }));
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
  server.listen(PORT, () => console.log(`${PROVIDER}-runner listening on :${PORT} (bin=${BIN})`));
}

module.exports = {
  tokenOk,
  safeCwd,
  enforcePii,
  geminiArgs,
  READ_ONLY_TOOLS,
};
