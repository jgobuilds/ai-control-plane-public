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
const EXTRA_ARGS = (process.env.EXTRA_ARGS || "--yolo --output-format json")
  .split(/\s+/)
  .filter(Boolean);

const WORKSPACE = "/workspace";
function safeCwd(rel) {
  if (!rel) return WORKSPACE;
  const p = path.resolve(WORKSPACE, rel);
  if (p !== WORKSPACE && !p.startsWith(WORKSPACE + path.sep))
    throw new Error(`cwd "${rel}" escapes the workspace`);
  if (!fs.existsSync(p)) throw new Error(`cwd "${rel}" does not exist in the workspace`);
  return p;
}

function runAgent({ prompt, model, cwd }) {
  return new Promise((resolve, reject) => {
    const args = [PROMPT_FLAG, prompt, ...EXTRA_ARGS];
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
      const { prompt, model, cwd } = JSON.parse(body || "{}");
      if (!prompt) return send(400, { error: "prompt is required" });
      send(200, await runAgent({ prompt, model, cwd }));
    } catch (e) {
      send(500, { error: String(e.message || e) });
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
};
