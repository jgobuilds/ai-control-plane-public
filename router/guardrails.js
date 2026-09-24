// Prompt-injection & output guardrails (threat-model H3 + the excessive-agency
// theme). Pure functions, no dependencies (core-only, like rbac.js/killswitch.js).
//
// This is a HEURISTIC layer, NOT a guarantee. Injection detection is undecidable
// in general; these patterns flag the obvious, well-known shapes (instruction
// override, role reassignment, exfiltration verbs, cross-scope path references,
// large opaque blobs) so the router can WARN or BLOCK per policy. The real
// isolation guarantee is structural (C1 per-scope mounts + per-scope tool
// allowlisting); treat this as defence-in-depth that raises the cost of an
// attack and produces audit signal — never as the sole control.
//
// Design rules:
//   * Conservative — aim to flag true injection while keeping false positives
//     modest. Blob heuristics are deliberately high-threshold and rated `low`
//     so they warn rather than block under the default policy.
//   * Every finding carries a severity `level` (low|medium|high). policy.json
//     `guardrails.minLevelToBlock` decides which levels actually block.
//   * Patterns live in clearly-labelled, tunable arrays below.
//   * A finding is { type, level, snippet }. The `snippet` is for the HTTP
//     response / operator triage ONLY — the router writes finding TYPES (never
//     the matched substring) to the audit ledger, keeping it metadata-only.
"use strict";

const http = require("http");
const https = require("https");

const LEVEL_RANK = { low: 1, medium: 2, high: 3 };

// Length thresholds for opaque-blob heuristics. High on purpose: normal prose
// never produces a 160-char run of base64 or a 128-char hex run, but embedded
// payloads / smuggled data do. Tune here.
const BASE64_MIN = 160;
const HEX_MIN = 128;

// ---------- INPUT (prompt) injection heuristics ----------
// Each entry: { type, level, re }. `re` must be global-free here (we call
// String.match per-entry and take the first hit for the snippet).
const INPUT_PATTERNS = [
  // Instruction-override / "ignore your instructions" family.
  { type: "instruction-override", level: "high",
    re: /ignore\s+(?:all\s+|the\s+)?(?:previous|above|prior|earlier|preceding|foregoing)\s+(?:instructions?|prompts?|directions?|messages?|context|commands?|rules?)/i },
  { type: "instruction-override", level: "high",
    re: /disregard\s+(?:all\s+|any\s+|the\s+|your\s+|previous\s+|prior\s+)?(?:instructions?|rules?|guidelines?|prompt|context|directions?)/i },
  { type: "instruction-override", level: "high",
    re: /forget\s+(?:everything|all|what|your|the|previous|prior|above)\b[^.\n]{0,40}(?:instructions?|said|told|rules?|context)?/i },
  { type: "instruction-override", level: "medium",
    re: /(?:override|bypass|turn\s+off|disable)\s+(?:your\s+|the\s+|all\s+)?(?:instructions?|rules?|guardrails?|safety|restrictions?|filters?|policies)/i },

  // Role reassignment / persona hijack.
  { type: "role-reassignment", level: "high",
    re: /you\s+are\s+now\b/i },
  { type: "role-reassignment", level: "medium",
    re: /(?:from\s+now\s+on|henceforth|going\s+forward)\s*,?\s*you\s+(?:are|will|must|should|shall)\b/i },
  { type: "role-reassignment", level: "medium",
    re: /(?:pretend|act)\s+(?:to\s+be|as\s+(?:if\s+)?(?:you(?:'re| are)|a\b))/i },
  { type: "role-reassignment", level: "medium",
    re: /(?:ignore|forget)\s+(?:your\s+)?(?:system\s+prompt|persona|role|identity)/i },

  // Jailbreak markers.
  { type: "jailbreak", level: "medium",
    re: /\b(?:developer\s+mode|do\s+anything\s+now|DAN\s+mode|jailbreak(?:en)?|unfiltered\s+mode|god\s+mode)\b/i },

  // Exfiltration verbs aimed at secrets / credentials / data.
  { type: "exfil-intent", level: "high",
    re: /\b(?:exfiltrate|leak|reveal|dump|steal|send|upload|post|transmit|email|forward|paste|publish)\b[^.\n]{0,40}\b(?:secrets?|credentials?|api[\s_-]?keys?|access[\s_-]?keys?|tokens?|passwords?|private[\s_-]?keys?|\.env|env(?:ironment)?\s+(?:vars?|variables?))\b/i },

  // Cross-scope / path-traversal references — an agent's prompt should not be
  // steering it up-and-over into another scope's tree.
  { type: "path-traversal", level: "medium",
    re: /\.\.[\/\\]/ },
  { type: "cross-scope-path", level: "medium",
    re: /(?:^|[^\w])\/?workspace\/scopes\/[A-Za-z0-9._-]+/i },

  // Opaque blobs (possible smuggled payloads). High threshold, low severity.
  { type: "base64-blob", level: "low",
    re: new RegExp(`[A-Za-z0-9+/]{${BASE64_MIN},}={0,2}`) },
  { type: "hex-blob", level: "low",
    re: new RegExp(`\\b(?:[0-9a-fA-F]{2}){${Math.ceil(HEX_MIN / 2)},}\\b`) },
];

// ---------- OUTPUT (runner result) secret/exfil heuristics ----------
const OUTPUT_PATTERNS = [
  { type: "private-key", level: "high",
    re: /-----BEGIN\s+(?:RSA|EC|DSA|OPENSSH|PGP|ENCRYPTED)?\s*PRIVATE\s+KEY-----/i },
  { type: "aws-access-key", level: "high",
    re: /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/ },
  { type: "google-api-key", level: "high",
    re: /\bAIza[0-9A-Za-z_-]{35}\b/ },
  { type: "github-token", level: "high",
    re: /\bgh[pousr]_[A-Za-z0-9]{30,}\b/ },
  { type: "slack-token", level: "high",
    re: /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/ },
  { type: "openai-key", level: "high",
    re: /\bsk-[A-Za-z0-9]{20,}\b/ },
  { type: "bearer-token", level: "medium",
    re: /\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*\b/ },
  { type: "jwt", level: "medium",
    re: /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/ },
  // Cross-scope filename leakage — a result referencing another scope's tree.
  { type: "cross-scope-path", level: "medium",
    re: /(?:^|[^\w])\/?workspace\/scopes\/[A-Za-z0-9._-]+/i },
];

// ---------- Operator-supplied pattern overlay ----------
//
// WHY. The arrays above ship in a public, brand-neutral repo, so anyone can read
// them — including anyone probing a deployment. That is an acceptable trade for
// the OUTPUT patterns: they match vendor credential FORMATS (AKIA…, ghp_…,
// sk-…), which are published by the vendors themselves and identical in every
// open scanner. Hiding them buys nothing.
//
// The INPUT patterns are different. Injection phrasings are our own work, and an
// attacker who can read the exact regex can phrase around it first try instead
// of on the twentieth. We cannot un-publish the defaults — but no deployment
// need be limited to them. An operator supplies additional (or replacement)
// patterns that never appear in this repo, resolved from `context/`, which is
// the same private-slot mechanism `context/brand.md` and `context/hygiene.json`
// already use. The engine still works, and still demonstrates the control, with
// no overlay present.
//
// Obscurity is NOT the boundary here, and this does not pretend otherwise — see
// the header: the real isolation guarantee is structural. This raises attacker
// cost and keeps the defender's specific tripwires out of a public repo.
//
// FILE. JSON: { "mode": "extend" | "replace", "input": [...], "output": [...] }
// Entry: { "type": "...", "level": "low|medium|high", "re": "...", "flags": "i" }
// Resolution: $GUARDRAIL_PATTERNS, else context/guardrail-patterns.json, else
// the committed neutral context/guardrail-patterns.example.json.
const fs = require("fs");
const path = require("path");

const OVERLAY_CANDIDATES = [
  process.env.GUARDRAIL_PATTERNS,
  path.join(__dirname, "..", "context", "guardrail-patterns.json"),
  "/app/context/guardrail-patterns.json",
  path.join(__dirname, "..", "context", "guardrail-patterns.example.json"),
  "/app/context/guardrail-patterns.example.json",
].filter(Boolean);

function warn(msg) {
  // Never throw from here: a malformed overlay must not take the router down,
  // and must not silently weaken it either. Say so, loudly, and keep built-ins.
  console.error(`[guardrails] ${msg}`);
}

function compileEntries(raw, where) {
  const out = [];
  if (!Array.isArray(raw)) return out;
  for (const e of raw) {
    if (!e || typeof e.re !== "string" || typeof e.type !== "string") {
      warn(`${where}: skipping entry without a string 'type' and 're'`);
      continue;
    }
    const level = LEVEL_RANK[e.level] ? e.level : "medium";
    if (!LEVEL_RANK[e.level]) warn(`${where}: '${e.type}' has no valid level; using medium`);
    // `scan()` uses String.match and reads m[0]; a global/sticky regex changes
    // that contract (see the note on INPUT_PATTERNS above), so refuse those.
    const flags = String(e.flags || "");
    if (/[gy]/.test(flags)) {
      warn(`${where}: '${e.type}' uses g/y flags, which break snippet capture — dropped`);
      continue;
    }
    if (!/^[imsu]*$/.test(flags)) {
      warn(`${where}: '${e.type}' has unsupported flags '${flags}' — dropped`);
      continue;
    }
    try {
      out.push({ type: e.type, level, re: new RegExp(e.re, flags) });
    } catch (err) {
      warn(`${where}: '${e.type}' has an invalid regex — dropped (${err.message})`);
    }
  }
  return out;
}

function applyOverlay() {
  const file = OVERLAY_CANDIDATES.find((p) => { try { return fs.statSync(p).isFile(); } catch { return false; } });
  if (!file) return;                      // no overlay is the normal case
  let cfg;
  try {
    cfg = JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (err) {
    warn(`${file}: unreadable or invalid JSON — keeping built-in patterns (${err.message})`);
    return;
  }
  const mode = cfg.mode === "replace" ? "replace" : "extend";
  const input = compileEntries(cfg.input, `${file} input`);
  const output = compileEntries(cfg.output, `${file} output`);
  if (!input.length && !output.length) return;   // an empty example file is fine

  for (const [target, add, label] of [[INPUT_PATTERNS, input, "input"], [OUTPUT_PATTERNS, output, "output"]]) {
    if (!add.length) continue;
    if (mode === "replace") {
      // A typo in `replace` must not silently disable a control. Refuse to leave
      // the set empty; the diagnosability lens: no silent empty results.
      target.length = 0;
    }
    target.push(...add);
    warn(`${label}: ${mode === "replace" ? "replaced with" : "extended by"} ${add.length} operator pattern(s) from ${path.basename(file)}`);
  }
}

applyOverlay();

// Trim a matched substring to a short, safe snippet for operator triage. This is
// returned to the caller but MUST NOT be persisted to the audit ledger.
function snippetOf(match) {
  const s = String(match || "").replace(/\s+/g, " ").trim();
  return s.length > 60 ? s.slice(0, 57) + "…" : s;
}

function scan(text, patterns) {
  const s = String(text || "");
  const findings = [];
  for (const { type, level, re } of patterns) {
    const m = s.match(re);
    if (m) findings.push({ type, level, snippet: snippetOf(m[0]) });
  }
  return findings;
}

// inputScan(prompt) -> [{ type, level, snippet }]
function inputScan(prompt) {
  return scan(prompt, INPUT_PATTERNS);
}

// outputScan(text) -> [{ type, level, snippet }]
// Workflows call this on runner output BEFORE shipping a deliverable (egress).
// The router also runs it on the verify/result text and attaches the findings as
// decision.outputFindings (see router/server.js). See GUARDRAILS.md for the
// workflow-side call pattern.
function outputScan(text) {
  return scan(text, OUTPUT_PATTERNS);
}

// Does any finding meet or exceed `minLevel`? Drives policy.guardrails.input:
// "block" — an unknown/blank minLevel falls back to "medium".
function atOrAbove(findings, minLevel) {
  const thr = LEVEL_RANK[minLevel] || LEVEL_RANK.medium;
  return (findings || []).some((f) => (LEVEL_RANK[f.level] || 0) >= thr);
}

// --- pluggable ML backend (E3) ---------------------------------------------
// DEFAULT = heuristic (the sync scans above), byte-identical when unconfigured.
// When policy.guardrails.backend is lakera|bedrock + backendUrl set, delegate to
// that detector; NO new deps (http/https only). FAIL-SAFE: any backend error
// falls back to the heuristic scan and marks backendError. Findings normalize to
// the same { type, level, snippet } shape. See DETECTION-BACKENDS.md.
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

// Normalize a backend response to [{ type, level, snippet }]. Handles a generic
// { findings: [{ type, level, snippet }] } shape and Lakera-style
// { results: [{ detector_type, flagged, ... }] } / { flagged: bool }.
function backendFindings(resp) {
  if (!resp) return [];
  if (Array.isArray(resp.findings)) {
    return resp.findings.map((f) => ({
      type: String(f.type || "backend"),
      level: LEVEL_RANK[f.level] ? f.level : "high",
      snippet: f.snippet ? snippetOf(f.snippet) : "",
    }));
  }
  const flagged = resp.flagged === true || (Array.isArray(resp.results) && resp.results.some((r) => r.flagged || r.detected));
  return flagged ? [{ type: "backend-flagged", level: "high", snippet: "" }] : [];
}

async function scanViaBackend(text, opts) {
  const resp = await httpPostJson(opts.backendUrl, { text: String(text || ""), backend: opts.backend }, opts.timeoutMs);
  return backendFindings(resp);
}

// Async input scan: heuristic default; backend (merged with heuristic) when set.
async function inputScanAsync(prompt, opts) {
  const heuristic = inputScan(prompt);
  if (!opts || !opts.backend || opts.backend === "heuristic" || !opts.backendUrl) return heuristic;
  try {
    return [...heuristic, ...(await scanViaBackend(prompt, opts))];
  } catch (e) {
    return heuristic.concat([{ type: "backend-error", level: "low", snippet: String(e.message || e) }]);
  }
}

async function outputScanAsync(text, opts) {
  const heuristic = outputScan(text);
  if (!opts || !opts.backend || opts.backend === "heuristic" || !opts.backendUrl) return heuristic;
  try {
    return [...heuristic, ...(await scanViaBackend(text, opts))];
  } catch (e) {
    return heuristic.concat([{ type: "backend-error", level: "low", snippet: String(e.message || e) }]);
  }
}

module.exports = {
  inputScan,
  outputScan,
  inputScanAsync,
  outputScanAsync,
  backendFindings,
  atOrAbove,
  // exported for tests / tuning transparency
  INPUT_PATTERNS,
  OUTPUT_PATTERNS,
  LEVEL_RANK,
};
