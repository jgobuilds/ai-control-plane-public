// PII scrubber + ingest gateway. Sits between Drive (via n8n) and the agent
// workspace. Deterministic, reversible tokenization — the local twin of Cloud
// DLP's CryptoDeterministicConfig (swap in DLP via SCRUB_BACKEND when on GCP).
//
// POST /scrub      { text, scope }            -> { text, findings }   (tokenize only)
// POST /ingest     { scope, filename, text }  -> scrub + write to the scope's dir
// POST /rehydrate  { text, scope }            -> { text }             (tokens -> real values)
// POST /outbox     { scope, rehydrate? }      -> pending deliverables, rehydrated for upload
// POST /outbox/ack { scope, filename }        -> archive a shipped deliverable to sent/
// POST /promote/preview { scope, file, toScope }        -> scrubbed skill text for review
// POST /promote/commit  { scope, file, toScope, text? } -> write reviewed text to target scope
// GET  /health
//
// THE CONTROL THAT MATTERS: token maps live in /vault, which is mounted ONLY
// into this container. Runners mount /workspace but never /vault, so the model
// sees «EMAIL_1» and physically cannot look up what it stands for. Rehydration
// is a post-model step (n8n calls /rehydrate on deliverables).
const http = require("http");
const https = require("https");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const PORT = 8080;
const TOKEN = process.env.SCRUBBER_TOKEN || "";
if (!TOKEN && process.env.ALLOW_NO_AUTH !== "1") {
  console.error("scrubber: SCRUBBER_TOKEN must be set (or ALLOW_NO_AUTH=1 for local dev only). Refusing to start.");
  process.exit(1);
}
function tokenOk(provided, expected) {
  if (!expected) return process.env.ALLOW_NO_AUTH === "1";
  const a = Buffer.from(String(provided || ""));
  const b = Buffer.from(expected);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
// Base dirs default to the container mount points; overridable via env ONLY so
// unit tests can point the vault/workspace at a temp dir. In the docker image
// these env vars are unset, so behavior is identical to the hardcoded paths.
const WORKSPACE = process.env.WORKSPACE_DIR || "/workspace";
const VAULT = process.env.VAULT_DIR || "/vault";
const SCOPES = JSON.parse(
  fs.readFileSync(process.env.SCOPES_PATH || "/app/context/scopes.json", "utf8")
);

// Ordered so overlapping digit patterns resolve correctly (card before phone).
// Regex covers structured PII; names/addresses need NER — use Cloud DLP or
// Presidio for that class (documented limitation, not a silent gap).
const PATTERNS = [
  { type: "CARD", re: /\b\d(?:[ -]?\d){12,15}\b/g },
  { type: "SSN", re: /\b\d{3}-\d{2}-\d{4}\b/g },
  { type: "PHONE", re: /\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b/g },
  { type: "EMAIL", re: /[\w.+-]+@[\w-]+\.[\w.-]+/g },
];

function resolveScope(name) {
  const node = SCOPES.nodes[name];
  if (!node) throw new Error(`unknown scope "${name}"`);
  const chain = [];
  let cur = name, guard = 0;
  while (cur) {
    if (++guard > 32) throw new Error("scope tree cycle");
    chain.unshift(cur);
    cur = SCOPES.nodes[cur]?.parent || null;
  }
  const controls = { ...(SCOPES.levelDefaults?.[node.level] || {}), ...(node.controls || {}) };
  return { name, node, chain, controls };
}

// --- per-scope deterministic token vault ---
function vaultPath(scope) { return path.join(VAULT, `${scope}.json`); }
function loadVault(scope) {
  try { return JSON.parse(fs.readFileSync(vaultPath(scope), "utf8")); }
  catch { return { byValue: {}, byToken: {}, counters: {} }; }
}
function saveVault(scope, v) {
  fs.mkdirSync(VAULT, { recursive: true });
  fs.writeFileSync(vaultPath(scope), JSON.stringify(v, null, 2));
}

function scrub(text, scope) {
  const v = loadVault(scope);
  const findings = {};
  let out = String(text || "");
  for (const { type, re } of PATTERNS) {
    out = out.replace(re, (match) => {
      const key = `${type}:${match}`;
      let token = v.byValue[key];
      if (!token) {
        v.counters[type] = (v.counters[type] || 0) + 1;
        token = `«${type}_${v.counters[type]}»`;   // same value -> same token, always
        v.byValue[key] = token;
        v.byToken[token] = match;
      }
      findings[type] = (findings[type] || 0) + 1;
      return token;
    });
  }
  if (Object.keys(findings).length) saveVault(scope, v);
  return { text: out, findings };
}

function rehydrate(text, scope) {
  const v = loadVault(scope);
  let restored = 0;
  const out = String(text || "").replace(/«[A-Z]+_\d+»/g, (t) => {
    if (v.byToken[t] !== undefined) { restored++; return v.byToken[t]; }
    return t; // unknown token stays visible — better loud than silently wrong
  });
  return { text: out, restored };
}

// --- pluggable PII detection backend (E3) -----------------------------------
// DEFAULT = heuristic (the regex `scrub` above), byte-identical when unset. When
// SCRUB_BACKEND is presidio|dlp, detection is delegated to an ML service that
// returns spans; the SAME deterministic vault then tokenizes them. NO new deps
// (Node http/https only). FAIL-SAFE: a backend error falls back to the heuristic
// detector and flags `backendError:true` — text is never passed through raw.
// See DETECTION-BACKENDS.md for the request/response shapes.
const SCRUB_BACKEND = process.env.SCRUB_BACKEND || "heuristic";
const SCRUB_BACKEND_URL = process.env.SCRUB_BACKEND_URL || "";
const SCRUB_BACKEND_TIMEOUT_MS = parseInt(process.env.SCRUB_BACKEND_TIMEOUT_MS || "5000", 10);

function httpPostJson(url, payload, timeoutMs) {
  return new Promise((resolve, reject) => {
    let u;
    try { u = new URL(url); } catch (e) { return reject(new Error("bad backend URL")); }
    const lib = u.protocol === "https:" ? https : http;
    const data = Buffer.from(JSON.stringify(payload));
    const req = lib.request(
      u,
      { method: "POST", headers: { "content-type": "application/json", "content-length": data.length } },
      (resp) => {
        let b = "";
        resp.on("data", (c) => (b += c));
        resp.on("end", () => {
          if (resp.statusCode < 200 || resp.statusCode >= 300) return reject(new Error("backend HTTP " + resp.statusCode));
          try { resolve(JSON.parse(b)); } catch (e) { reject(new Error("backend returned non-JSON")); }
        });
      }
    );
    req.on("error", reject);
    req.setTimeout(timeoutMs || 5000, () => req.destroy(new Error("backend timeout")));
    req.write(data);
    req.end();
  });
}

// Normalize a backend response to [{ type, value }] spans of literal PII text.
// Handles Presidio's analyzer shape (start/end/entity_type over the input text)
// and a generic { entities: [{ type, value }] } shape.
function backendSpans(text, backend, resp) {
  const out = [];
  const arr = Array.isArray(resp) ? resp : (resp && resp.entities) || (resp && resp.results) || [];
  for (const e of arr) {
    if (e == null) continue;
    if (typeof e.start === "number" && typeof e.end === "number") {
      const value = String(text).slice(e.start, e.end);
      if (value) out.push({ type: String(e.entity_type || e.type || "PII").toUpperCase(), value });
    } else if (e.value) {
      out.push({ type: String(e.type || e.entity_type || "PII").toUpperCase(), value: String(e.value) });
    }
  }
  return out;
}

async function detectPiiBackend(text) {
  const body =
    SCRUB_BACKEND === "presidio"
      ? { text: String(text || ""), language: "en" }
      : { text: String(text || "") }; // dlp / generic adapter
  const resp = await httpPostJson(SCRUB_BACKEND_URL, body, SCRUB_BACKEND_TIMEOUT_MS);
  return backendSpans(text, SCRUB_BACKEND, resp);
}

// Deterministic tokenization from a list of {type,value} spans — same vault
// semantics as the heuristic path (same value -> same token, persisted). Longest
// values first so a longer PII string isn't broken by a shorter substring token.
function tokenizeSpans(text, scope, matches) {
  const v = loadVault(scope);
  const findings = {};
  let out = String(text || "");
  const uniq = [...new Map(matches.filter((m) => m && m.value).map((m) => [`${m.type}:${m.value}`, m])).values()]
    .sort((a, b) => b.value.length - a.value.length);
  for (const { type, value } of uniq) {
    const key = `${type}:${value}`;
    let token = v.byValue[key];
    if (!token) {
      v.counters[type] = (v.counters[type] || 0) + 1;
      token = `«${type}_${v.counters[type]}»`;
      v.byValue[key] = token;
      v.byToken[token] = value;
    }
    const before = out;
    out = out.split(value).join(token);
    if (out !== before) findings[type] = (findings[type] || 0) + 1;
  }
  if (Object.keys(findings).length) saveVault(scope, v);
  return { text: out, findings };
}

// The seam every writer path uses. Heuristic (default) is byte-identical to
// scrub(); a configured backend is used with heuristic fallback on any error.
async function scrubAsync(text, scope) {
  if (SCRUB_BACKEND === "heuristic" || !SCRUB_BACKEND_URL) return scrub(text, scope);
  try {
    return tokenizeSpans(text, scope, await detectPiiBackend(text));
  } catch (e) {
    return { ...scrub(text, scope), backendError: String(e.message || e) };
  }
}

// --- outbound deliverables ---
// Agents write TOKENIZED files to <scope>/deliverables/. The outbox lists them
// (rehydrated on request — PII re-enters only here, post-model, never in a
// prompt); after a successful upload the caller acks and the tokenized original
// is archived to deliverables/sent/ so nothing ships twice.
function deliverablesDir(scope) {
  const s = resolveScope(scope);
  if (s.controls.isolation === "silo")
    throw new Error(`scope "${scope}" is silo-isolated; use its dedicated runner's scrubber`);
  return path.join(WORKSPACE, SCOPES.workspaceRoot || "scopes", ...s.chain, "deliverables");
}

function outbox(scope, doRehydrate = true) {
  const dir = deliverablesDir(scope);
  if (!fs.existsSync(dir)) return { files: [] };
  const files = [];
  for (const name of fs.readdirSync(dir)) {
    const p = path.join(dir, name);
    if (!fs.statSync(p).isFile()) continue; // skips sent/ and any subdirs
    const raw = fs.readFileSync(p, "utf8");
    const hyd = doRehydrate ? rehydrate(raw, scope) : { text: raw, restored: 0 };
    files.push({ filename: name, text: hyd.text, restored: hyd.restored,
      mtime: fs.statSync(p).mtime.toISOString() });
  }
  return { files };
}

function ackDeliverable(scope, filename) {
  const dir = deliverablesDir(scope);
  const src = path.join(dir, path.basename(String(filename || "")));
  if (!fs.existsSync(src) || !fs.statSync(src).isFile())
    throw new Error(`no pending deliverable "${filename}" in scope "${scope}"`);
  const sent = path.join(dir, "sent");
  fs.mkdirSync(sent, { recursive: true });
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const dest = path.join(sent, `${stamp}__${path.basename(src)}`);
  fs.renameSync(src, dest);
  return { archived: dest.replace(WORKSPACE, "workspace") };
}

async function ingest(scope, filename, text) {
  const s = resolveScope(scope);
  // Silo scopes' data must NEVER land in the shared workspace — their sync runs
  // against their dedicated runner's own workspace/scrubber pair.
  if (s.controls.isolation === "silo")
    throw new Error(`scope "${scope}" is silo-isolated; ingest via its dedicated runner's scrubber, not the shared one`);
  const clean = await scrubAsync(text, scope);
  const safeName = path.basename(String(filename || "unnamed")).replace(/[^\w.\- ]/g, "_");
  const dir = path.join(WORKSPACE, SCOPES.workspaceRoot || "scopes", ...s.chain, "ingest");
  fs.mkdirSync(dir, { recursive: true });
  const dest = path.join(dir, safeName.endsWith(".md") || safeName.includes(".") ? safeName : safeName + ".md");
  fs.writeFileSync(dest, clean.text);
  return { written: dest.replace(WORKSPACE, "workspace"), findings: clean.findings };
}

// --- skill promotion (the ONLY sanctioned path from narrow -> wide) ---
// Two-phase: preview returns the SCRUBBED text for human review (generalization
// can't be automated — a human edits out client-recognizable specifics); commit
// writes the reviewed text into the target scope with provenance. Promotion is
// UP the tree only: toScope must be a strict ancestor of scope.
function scopeDir(chain, ...sub) {
  return path.join(WORKSPACE, SCOPES.workspaceRoot || "scopes", ...chain, ...sub);
}

async function promotePreview(scope, file, toScope) {
  const s = resolveScope(scope);
  const t = resolveScope(toScope);
  if (!(s.chain.includes(toScope) && toScope !== scope))
    throw new Error(`"${toScope}" is not an ancestor of "${scope}" — promotion only goes UP the tree`);
  const src = path.join(scopeDir(s.chain, "skills"), path.basename(String(file || "")));
  if (!fs.existsSync(src)) throw new Error(`no skill "${file}" in ${scope}/skills/`);
  const clean = await scrubAsync(fs.readFileSync(src, "utf8"), scope);
  return { from: scope, to: toScope, file: path.basename(src), text: clean.text,
    findings: clean.findings,
    note: "Review + generalize before commit: remove client-recognizable specifics the scrub can't detect (names of projects, unusual numbers, quotes)." };
}

async function promoteCommit(scope, file, toScope, reviewedText) {
  const preview = await promotePreview(scope, file, toScope); // re-validates everything
  const t = resolveScope(toScope);
  const text = reviewedText || preview.text;
  // Refuse commit if raw PII somehow reappeared in the reviewed text.
  const recheck = await scrubAsync(text, scope);
  if (Object.keys(recheck.findings).length)
    throw new Error(`reviewed text still contains raw PII (${Object.keys(recheck.findings).join(", ")}) — scrub it before committing`);
  const dir = scopeDir(t.chain, "skills");
  fs.mkdirSync(dir, { recursive: true });
  const dest = path.join(dir, preview.file);
  const provenance = `<!-- promoted from scope "${scope}" on ${new Date().toISOString()}; reviewed via promotion gate -->\n`;
  fs.writeFileSync(dest, provenance + text);
  return { written: dest.replace(WORKSPACE, "workspace"), from: scope, to: toScope };
}

const server = http.createServer((req, res) => {
  const send = (code, obj) =>
    res.writeHead(code, { "content-type": "application/json" }).end(JSON.stringify(obj));
  if (req.method === "GET" && req.url === "/health") return res.writeHead(200).end("ok");
  if (req.method !== "POST") return res.writeHead(404).end("not found");
  if (!tokenOk(req.headers["x-scrubber-token"], TOKEN)) return res.writeHead(401).end("unauthorized");

  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", async () => {
    try {
      const { text, scope, filename, file, toScope, rehydrate: doRehydrate } = JSON.parse(body || "{}");
      if (!scope) return send(400, { error: "scope is required" });
      resolveScope(scope); // validate even for scrub/rehydrate
      if (req.url === "/scrub") return send(200, await scrubAsync(text, scope));
      if (req.url === "/rehydrate") return send(200, rehydrate(text, scope));
      if (req.url === "/ingest") return send(200, await ingest(scope, filename, text));
      if (req.url === "/outbox") return send(200, outbox(scope, doRehydrate !== false));
      if (req.url === "/outbox/ack") return send(200, ackDeliverable(scope, filename));
      if (req.url === "/promote/preview") return send(200, await promotePreview(scope, file, toScope));
      if (req.url === "/promote/commit") return send(200, await promoteCommit(scope, file, toScope, text));
      send(404, { error: "not found" });
    } catch (e) {
      send(500, { error: String(e.message || e) });
    }
  });
});

// Start the HTTP server ONLY when run directly; export pure helpers for tests.
if (require.main === module) {
  server.listen(PORT, () => console.log(`scrubber listening on :${PORT}`));
}

module.exports = {
  tokenOk,
  resolveScope,
  scrub,
  scrubAsync,
  detectPiiBackend,
  backendSpans,
  tokenizeSpans,
  rehydrate,
  ingest,
  outbox,
  ackDeliverable,
  promotePreview,
  promoteCommit,
};
