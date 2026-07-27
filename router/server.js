// Cost-tiering + context-scope router. The single entry point n8n calls; it
// enforces policy so neither cost nor isolation decisions can be bypassed.
// Order of operations per request:
//   0. Resolve + validate the context SCOPE (scopes.json): compute the cwd
//      chain, enforce ethical walls (conflicts), run the PII prompt guard.
//   1. Deterministic rules (Tier 0) — no LLM if a rule matches.
//   2. Resolve tier: explicit task_type -> policy map, else cheap triage.
//   3. Clamp to the policy's maxTier ceiling (cost guardrail).
//   4. Dispatch to the cheapest capable runner/model, cwd-scoped to the node.
//   5. Optionally cross-verify on the OTHER provider (independent fresh context).
// No dependencies — Node http only.
const http = require("http");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const PORT = 8080;
const RUNNER_TOKEN = process.env.RUNNER_TOKEN || "";   // outbound, to the runners
const ROUTER_TOKEN = process.env.ROUTER_TOKEN || "";   // inbound, from n8n

// Fail closed: refuse to boot without auth unless explicitly opted out for dev.
if ((!ROUTER_TOKEN || !RUNNER_TOKEN) && process.env.ALLOW_NO_AUTH !== "1") {
  console.error("router: ROUTER_TOKEN and RUNNER_TOKEN must be set (or ALLOW_NO_AUTH=1 for local dev only). Refusing to start.");
  process.exit(1);
}
// Constant-time token check; empty expected token never matches (no fail-open).
function tokenOk(provided, expected) {
  if (!expected) return process.env.ALLOW_NO_AUTH === "1";
  const a = Buffer.from(String(provided || ""));
  const b = Buffer.from(expected);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}
const POLICY = JSON.parse(fs.readFileSync(process.env.POLICY_PATH || "/app/policy.json", "utf8"));
const RULES = require(process.env.RULES_PATH || "/app/rules.js");
const rbac = require("./rbac.js");   // approver RBAC + segregation of duties (M5)
const killswitch = require("./killswitch.js");   // kill switch + budget/rate circuit breaker
const guard = require("./guardrails.js");   // prompt-injection + output guardrails (H3)

// Scope registry is optional — without it the router behaves as before.
let SCOPES = null;
try {
  SCOPES = JSON.parse(fs.readFileSync(process.env.SCOPES_PATH || "/app/context/scopes.json", "utf8"));
} catch { /* no scopes.json mounted — scope enforcement disabled */ }

const TIER_ORDER = ["t0", "t1", "t2", "t3"];
const idx = (t) => TIER_ORDER.indexOf(t);

// ---------- tamper-evident audit ledger ----------
// Append-only, hash-chained record of every decision. Metadata ONLY — never the
// prompt (a SHA-256 + length instead) and never PII values (types + counts). Line
// format: "<hash> <json>", where hash = sha256(prevHash + json). Re-hashing the
// raw json substring makes it verifiable from any language (scripts/verify_audit.py).
const AUDIT_DIR = process.env.AUDIT_DIR || "/audit";
const AUDIT_LOG = path.join(AUDIT_DIR, "decisions.jsonl");
const sha256 = (s) => crypto.createHash("sha256").update(s).digest("hex");
function lastHash() {
  try {
    const t = fs.readFileSync(AUDIT_LOG, "utf8").trimEnd();
    if (!t) return "GENESIS";
    const line = t.slice(t.lastIndexOf("\n") + 1);
    return line.slice(0, line.indexOf(" "));
  } catch { return "GENESIS"; }
}
function auditRecord(input, out) {
  const d = out.decision || {};
  const prevHash = lastHash();
  const rec = {
    ts: new Date().toISOString(),
    id: crypto.randomUUID(),
    scope: d.scope ?? input.scope ?? null,
    mode: d.mode ?? null,
    advisory: d.advisory ?? null,
    requiresOutputValidation: d.requiresOutputValidation ?? null,
    risk: d.risk ? { level: d.risk.level, dims: d.risk.dims, controls: d.risk.controls } : null,
    action: input.action ?? "advise",
    trigger: input.trigger ?? "turn",
    approved: !!input.approved,
    requester: input.requester ?? null,
    approver: input.approver ?? null,
    approvalAuthorized: d.approvalAuthorized ?? null,
    blocked: out.blocked ?? null,
    tier: d.tier ?? null, provider: d.provider ?? null, model: d.model ?? null,
    pii: d.pii?.hits ? { types: d.pii.hits.map((h) => h.type), total: d.pii.hits.reduce((a, h) => a + h.count, 0) } : null,
    // Guardrail findings — TYPES + count ONLY (never the matched prompt/output
    // substring), keeping the ledger metadata-only like the PII record above.
    guardrails: d.guardrails?.length ? { types: d.guardrails.map((f) => f.type), total: d.guardrails.length } : null,
    outputFindings: d.outputFindings?.length ? { types: d.outputFindings.map((f) => f.type), total: d.outputFindings.length } : null,
    verdict: out.verdict?.pass ?? null,
    // Economics. notionalCostUsd is what this WOULD cost at metered API rates —
    // runners are subscription-billed, so it is a comparable yardstick, not
    // money spent. See AIOPS.md before putting these on a dashboard.
    usage: out.usage
      ? {
          notionalCostUsd: out.usage.notionalCostUsd ?? null,
          inputTokens: out.usage.inputTokens ?? null,
          outputTokens: out.usage.outputTokens ?? null,
          totalInputTokens: out.usage.totalInputTokens ?? null,
          turns: out.usage.turns ?? null,
          durationMs: out.usage.durationMs ?? null,
        }
      : null,
    promptSha256: sha256(String(input.prompt || "")),
    promptLen: String(input.prompt || "").length,
    prevHash,
  };
  const json = JSON.stringify(rec);
  fs.mkdirSync(AUDIT_DIR, { recursive: true });
  fs.appendFileSync(AUDIT_LOG, sha256(prevHash + json) + " " + json + "\n");
}
// Serialize writes so the hash chain can't fork under concurrent requests.
let auditQueue = Promise.resolve();
function audit(input, out) {
  auditQueue = auditQueue.then(() => { try { auditRecord(input, out); } catch (e) { console.error("audit:", e.message); } });
  return auditQueue;
}

// ---------- scope resolution (level 0 of the pipeline) ----------
function resolveScope(name) {
  const node = SCOPES.nodes[name];
  if (!node) throw new Error(`unknown scope "${name}" — not in the engagement/scope registry`);
  // Walk to root to build the ancestry chain and the nested directory path.
  const chain = [];
  let cur = name, guard = 0;
  while (cur) {
    if (++guard > 32) throw new Error("scope tree cycle detected");
    chain.unshift(cur);
    cur = SCOPES.nodes[cur]?.parent || null;
  }
  const controls = {
    ...(SCOPES.levelDefaults?.[node.level] || {}),
    ...(node.controls || {}),
  };
  // cwd is the node's nested dir: workspace/<root>/<ancestor>/…/<node>.
  // Claude Code loads CLAUDE.md upward from cwd, so ancestors' context is
  // inherited structurally; siblings simply aren't on the path.
  const cwd = path.posix.join(SCOPES.workspaceRoot || "scopes", ...chain);
  return { name, node, chain, controls, cwd };
}

function checkConflicts(scope) {
  const pairs = SCOPES.conflicts?.pairs || [];
  const inWall = pairs.some((p) => p.includes(scope.name));
  if (inWall && scope.controls.isolation !== "silo")
    throw new Error(`scope "${scope.name}" is behind an ethical wall and must be isolation:"silo"`);
  if (scope.controls.isolation === "silo" && !scope.node.runnerHost)
    throw new Error(`scope "${scope.name}" requires a dedicated runner (set runnerHost in scopes.json)`);
}

// Deterministic PII guard on the OUTBOUND prompt (belt and suspenders — ingest
// scrubbing is the primary control; see CONTEXT-ARCHITECTURE.md). Local regex
// baseline; swap/augment with Cloud DLP inspect for NER-grade coverage.
const PII_PATTERNS = [
  { type: "email", re: /[\w.+-]+@[\w-]+\.[\w.-]+/g },
  { type: "ssn", re: /\b\d{3}-\d{2}-\d{4}\b/g },
  { type: "phone", re: /\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b/g },
  { type: "card", re: /\b\d(?:[ -]?\d){12,15}\b/g },
];
function piiScan(text) {
  const hits = [];
  for (const { type, re } of PII_PATTERNS) {
    const m = String(text || "").match(re);
    if (m) hits.push({ type, count: m.length });
  }
  return hits;
}

// ---------- runner dispatch ----------
function callRunner(hostOrProvider, payload) {
  // Accepts a provider name ("claude" -> claude-runner) or a full host
  // (a silo scope's dedicated runnerHost).
  const host = hostOrProvider.includes("-runner") ? hostOrProvider : `${hostOrProvider}-runner`;
  const data = JSON.stringify(payload);
  const opts = {
    host, port: 8080, path: "/run", method: "POST",
    headers: {
      "content-type": "application/json",
      "content-length": Buffer.byteLength(data),
      "x-runner-token": RUNNER_TOKEN,
    },
  };
  return new Promise((resolve, reject) => {
    const r = http.request(opts, (resp) => {
      let b = "";
      resp.on("data", (c) => (b += c));
      resp.on("end", () => { try { resolve(JSON.parse(b)); } catch { resolve({ result: b }); } });
    });
    r.on("error", reject);
    r.setTimeout(16 * 60 * 1000, () => r.destroy(new Error("runner timeout")));
    r.write(data);
    r.end();
  });
}

function textOf(resp) {
  const r = resp && resp.result;
  if (r && typeof r === "object") return r.result ?? r.text ?? JSON.stringify(r);
  return r;
}

// ---------- tier resolution (unchanged logic) ----------
function tryRules(input) {
  for (const rule of RULES) {
    try { if (rule.match(input)) return { rule: rule.name, ...rule.run(input) }; }
    catch { /* a broken rule shouldn't block routing */ }
  }
  return null;
}

async function triage(prompt) {
  const tier = POLICY.tiers[POLICY.defaults.triageTier];
  const labels = Object.keys(POLICY.taskTypes).join(", ");
  const out = await callRunner(tier.provider, {
    prompt: `Classify this task as exactly one of: ${labels}. Reply with only the label.\n\nTASK:\n${prompt}`,
    model: tier.model,
  });
  const label = String(textOf(out) || "").trim().toLowerCase().split(/\s+/)[0];
  return POLICY.taskTypes[label] ? label : null;
}

function resolveTier({ task_type, requested }) {
  const allowOverride = POLICY.controls.allowRequestOverride;
  if (allowOverride && requested?.tier) return { tier: requested.tier, why: "request-override" };
  if (task_type && POLICY.taskTypes[task_type])
    return { tier: POLICY.taskTypes[task_type].tier, why: `taskType:${task_type}` };
  return null;
}

function clampTier(tier, { allowFrontier }) {
  const max = POLICY.controls.maxTier;
  if (idx(tier) > idx(max) && !allowFrontier) return { tier: max, clamped: true };
  return { tier, clamped: false };
}

// Pull usage from a runner response regardless of shape. claude-runner returns
// { result, verdict, usage }; older builds nested it under result. A runner with
// no economics (gemini today) yields null, which stays null rather than 0.
function usageOf(x) {
  if (!x || typeof x !== "object") return null;
  if (x.usage && typeof x.usage === "object") return x.usage;
  if (x.result && typeof x.result === "object" && x.result.usage) return x.result.usage;
  return null;
}

// Sum two usage records (executor + verifier). Nulls stay null rather than
// becoming 0, so "not measured" is never silently reported as "free".
function mergeUsage(a, b) {
  if (!a && !b) return null;
  const add = (x, y) => (x == null && y == null ? null : (x || 0) + (y || 0));
  const keys = ["notionalCostUsd", "inputTokens", "outputTokens",
                "totalInputTokens", "turns", "durationMs"];
  const out = {};
  for (const k of keys) out[k] = add(a && a[k], b && b[k]);
  if (b) out.verifyNotionalCostUsd = (b.notionalCostUsd ?? null);
  return out;
}

function safeParse(s) { try { return JSON.parse(s); } catch { return { pass: null, reasons: ["bad json"] }; } }

// ---------- risk classification (data x access x autonomy) ----------
// Consensus model (NIST AI RMF / Databricks DASF): an agent's risk lives in what
// it TOUCHES (data), what it can DO (access/blast radius), and how AUTONOMOUSLY
// it acts. We score each 1-3 and switch on proportional controls.
function classifyRisk(input, scope) {
  const R = POLICY.risk;
  if (!R) return null;
  const ctl = scope?.controls || {};
  const dataKey = ctl.isolation === "silo" ? "silo" : (ctl.pii || "default");
  const dims = {
    data: R.data[dataKey] ?? R.data.default,
    access: R.access[input.action] ?? R.access.default,       // advise|write|apply|send|…
    autonomy: R.autonomy[input.trigger] ?? R.autonomy.default, // turn|scheduled|proactive
  };
  const base = Math.max(dims.data, dims.access, dims.autonomy);
  const highs = Object.values(dims).filter((v) => v >= 3).length;
  const level = R.levels[Math.max(0, Math.min(3, highs >= 2 ? 3 : base - 1))];
  const on = (spec) => !!spec && Object.entries(spec).some(([d, thr]) => dims[d] >= thr);
  const cbd = R.controlsByDimension || {};
  return {
    level, dims,
    controls: {
      requireVerify: on(cbd.requireVerify),
      requireApproval: on(cbd.requireApproval),
      requireIsolation: on(cbd.requireIsolation),
      blockCrossVendor: on(cbd.blockCrossVendor),
    },
  };
}

// ---------- operating mode (who leads / who validates) ----------
const ACTION_RANK = { advise: 1, read: 1, write: 2, ingest: 2, apply: 3, send: 3, execute: 3 };
// Effective mode = scope's declared mode (its deliberate floor) tightened by any
// stricter request mode. A request can never RELAX below the scope's choice.
function resolveMode(input, scope) {
  const M = POLICY.modes;
  if (!M) return null;
  const order = M.order || [];
  const scopeMode = scope?.controls?.mode;
  const base = order.includes(scopeMode) ? scopeMode : M.default;
  let name = base;
  if (order.includes(input.mode) && order.indexOf(input.mode) > order.indexOf(base)) name = input.mode;
  return { name, ...(M.map[name] || {}) };
}

// ---------- the pipeline ----------
async function route(input) {
  // 0-. Kill switch + circuit breaker — evaluated BEFORE any scope work or
  // dispatch, so an operator halt or a runaway-rate trip stops the request cold.
  // The handler audits this return like any other decision (blocked:"halted"|
  // "circuit-open"). Resolve the scope CHAIN best-effort so an ancestor halt
  // catches descendants; an unknown scope falls back to the bare name and is
  // reported by the normal pipeline below.
  {
    const scopeName = (SCOPES ? (input.scope || SCOPES.defaultScope) : input.scope) || null;
    let scopeChain = scopeName ? [scopeName] : [];
    if (SCOPES && scopeName) {
      try { scopeChain = resolveScope(scopeName).chain; } catch { /* unknown scope: bare-name fallback */ }
    }
    const halt = killswitch.isHalted(killswitch.haltState(process.env.CONTROL_DIR), scopeChain);
    if (halt.halted)
      return { decision: { scope: scopeName }, blocked: "halted", reason: halt.reason };
    const brk = killswitch.breaker(POLICY, AUDIT_LOG, scopeName);
    if (brk.tripped)
      return { decision: { scope: scopeName }, blocked: "circuit-open", reason: brk.reason };
  }

  // 0. scope
  let scope = null, pii = null;
  if (SCOPES) {
    scope = resolveScope(input.scope || SCOPES.defaultScope);
    checkConflicts(scope);
    const hits = piiScan(input.prompt);
    if (hits.length) {
      pii = { hits, policy: scope.controls.pii, allowPii: !!input.allowPii };
      if (scope.controls.pii === "block" && !input.allowPii)
        throw new Error(
          `PII detected in prompt (${hits.map((h) => h.type).join(", ")}) and scope "${scope.name}" ` +
          `blocks raw PII. Scrub/tokenize it, or pass allowPii:true via the approved exception lane.`
        );
    }
  }

  // 0a. Prompt-injection guardrails (threat-model H3). Heuristic scan of the
  // OUTBOUND prompt; behaviour is policy-driven (POLICY.guardrails). On block we
  // return blocked:"guardrail-input" (audited). On warn we attach findings to the
  // decision and proceed. Only finding TYPES reach the audit ledger — never the
  // matched prompt substring — so the ledger stays metadata-only.
  let guardrails = null;
  {
    const G = POLICY.guardrails;
    if (G && G.input && G.input !== "off") {
      const findings = await guard.inputScanAsync(input.prompt, { backend: G.backend, backendUrl: G.backendUrl });
      if (findings.length) {
        guardrails = findings;
        if (G.input === "block" && guard.atOrAbove(findings, G.minLevelToBlock)) {
          return {
            decision: { scope: scope?.name || null, pii, guardrails: findings },
            blocked: "guardrail-input",
            message: `input guardrail blocked prompt (${findings.map((f) => f.type).join(", ")}). Treat ingested content as data, not instructions; see GUARDRAILS.md.`,
          };
        }
      }
    }
  }
  const guardStamp = guardrails ? { guardrails } : {};

  // 0b. risk classification + proportional-control enforcement
  const risk = classifyRisk(input, scope);
  if (risk) {
    if (risk.controls.requireIsolation && scope?.controls?.isolation !== "silo") {
      if (POLICY.risk.enforce?.requireIsolation === "block")
        throw new Error(`risk=${risk.level}: high-sensitivity data must run in an isolation:"silo" scope; "${scope?.name}" is not.`);
      risk.isolationWarning = `scope "${scope?.name}" handles high-sensitivity data but isn't isolation:"silo" (enforce=warn)`;
    }
    // A human must approve before an acting/self-triggered request runs.
    if (risk.controls.requireApproval && !input.approved) {
      return {
        decision: { scope: scope?.name || null, risk, action: input.action || "advise", trigger: input.trigger || "turn", pii },
        blocked: "approval-required",
        message: `risk=${risk.level} (action=${input.action || "advise"}, autonomy=${input.trigger || "turn"}). Route through the approval gate, then re-call with approved:true.`,
      };
    }
  }

  // 0b-rbac. If this request claims approval, enforce approver RBAC + segregation
  // of duties (M5): the named approver must be in an identityGroup authorized to
  // approve at this risk level/scope, and (high/critical) must differ from the
  // requester. Replaces "anyone with the unauthenticated resume URL can approve".
  if (input.approved) {
    const authz = rbac.authorizedToApprove({
      requester: input.requester,
      approver: input.approver,
      approverGroups: input.approverGroups,
      scope: scope?.name || null,
      riskLevel: risk?.level || null,
    }, POLICY);
    if (!authz.ok) {
      return {
        decision: {
          scope: scope?.name || null, risk,
          action: input.action || "advise", trigger: input.trigger || "turn",
          requester: input.requester || null, approver: input.approver || null,
          approvalAuthorized: false, pii,
        },
        blocked: "approval-unauthorized",
        message: `approval rejected: ${authz.reason}`,
      };
    }
  }

  // 0c. operating mode — cap what the agent may DO, and record the human's role.
  const mode = resolveMode(input, scope);
  if (mode) {
    const cap = ACTION_RANK[mode.maxAction] ?? 3;
    const req = ACTION_RANK[input.action] ?? 1;
    if (req > cap) {
      return {
        decision: { scope: scope?.name || null, mode: mode.name, risk, action: input.action || "advise" },
        blocked: "mode-forbids-action",
        message: `operating mode "${mode.name}" caps the agent at "${mode.maxAction}"; "${input.action}" must be performed by a human, not the agent.`,
      };
    }
  }
  // Stamps the workflow acts on: is the output advisory (human is the actor), or
  // must it pass human validation before it's authoritative (AI-led support)?
  const modeStamp = mode && {
    mode: mode.name,
    advisory: mode.humanValidation === "actor",
    requiresOutputValidation: mode.humanValidation === "output",
  };

  // 1. deterministic
  const det = tryRules(input);
  if (det) {
    return {
      decision: { tier: "t0", deterministic: true, rule: det.rule, scope: scope?.name || null, pii, risk, ...modeStamp, ...guardStamp },
      result: { result: det.text },
      verdict: null,
      usage: { notionalCostUsd: 0, inputTokens: 0, outputTokens: 0, turns: 0 },
    };
  }

  // 2. resolve tier
  let task_type = input.task_type;
  let resolved = resolveTier({ task_type, requested: input });
  let viaTriage = false;
  if (!resolved) {
    if (POLICY.defaults.unknownTaskType === "triage") {
      const t = await triage(input.prompt || "");
      if (t) { task_type = t; viaTriage = true; resolved = resolveTier({ task_type, requested: input }); }
    }
    if (!resolved) resolved = { tier: POLICY.defaults.fallbackTier, why: "fallback" };
  }

  // 3. clamp to cost ceiling
  const { tier: tierId, clamped } = clampTier(resolved.tier, { allowFrontier: !!input.allowFrontier });
  const tier = POLICY.tiers[tierId];

  const allowOverride = POLICY.controls.allowRequestOverride;
  const provider = (allowOverride && input.provider) || tier.provider;
  const model = (allowOverride && input.model) || tier.model;

  // 4. dispatch — silo scopes go to their dedicated runner; pool scopes get a
  // cwd inside the shared runner's workspace.
  const target = scope?.controls.isolation === "silo" ? scope.node.runnerHost : provider;
  // Per-scope tool allowlisting (excessive-agency): pass the resolved scope's
  // allowedTools (if any) to the runner. Unlisted tools are effectively denied —
  // defence-in-depth ON TOP of the firewall/mounts, not a replacement for them.
  const result = await callRunner(target, {
    prompt: input.prompt, model, maxTurns: input.maxTurns,
    cwd: scope && scope.controls.isolation !== "silo" ? scope.cwd : undefined,
    allowedTools: scope?.controls?.allowedTools,
  });

  // Output guardrail: scan the runner result text for secret/exfil patterns and
  // attach as decision.outputFindings (types only reach the audit ledger). This
  // is the router-side convenience check; workflows still call outputScan at the
  // deliverables egress before shipping (see GUARDRAILS.md).
  let outputFindings = null;
  {
    const G = POLICY.guardrails;
    if (G && G.output && G.output !== "off") {
      const of = await guard.outputScanAsync(textOf(result), { backend: G.backend, backendUrl: G.backendUrl });
      if (of.length) outputFindings = of;
    }
  }

  // 5. verify? (risk can force it; confidential data keeps the reviewer on the
  // SAME vendor so work isn't sent to a second provider)
  const ttVerify = task_type && POLICY.taskTypes[task_type]?.verify === "cross";
  const tierVerify = (POLICY.verifyOnTiers || []).includes(tierId);
  let verdict = null;
  let verifyUsage = null;
  if (input.verify || ttVerify || tierVerify || risk?.controls.requireVerify) {
    const other = risk?.controls.blockCrossVendor
      ? provider
      : (provider === "claude" ? "gemini" : "claude");
    const otherTier = Object.values(POLICY.tiers).find((t) => t.provider === other && t.kind === "model");
    const reviewPrompt = [
      "You are a strict, independent reviewer with no prior context.",
      "Judge ONLY whether the work satisfies the goal. Do not fix it.",
      'Reply with one JSON object: { "pass": true|false, "reasons": ["..."] }',
      "",
      `GOAL:\n${input.goal || "(judge for correctness and completeness)"}`,
      "",
      `WORK:\n${textOf(result)}`,
    ].join("\n");
    const vr = await callRunner(other, { prompt: reviewPrompt, model: otherTier?.model });
    verifyUsage = usageOf(vr);
    const m = String(textOf(vr) || "").match(/\{[\s\S]*\}/);
    verdict = m ? safeParse(m[0]) : { pass: null, reasons: ["unparseable verdict"] };
    verdict = { ...verdict, verifiedBy: other };
  }

  return {
    decision: {
      tier: tierId, provider, model, task_type: task_type || null, viaTriage, clamped,
      why: resolved.why, scope: scope?.name || null, scopeCwd: scope?.cwd || null,
      isolation: scope?.controls.isolation || null, pii, risk, ...modeStamp, ...guardStamp,
      allowedTools: scope?.controls?.allowedTools || null,
      ...(outputFindings ? { outputFindings } : {}),
      requester: input.requester || null, approver: input.approver || null,
      approvalAuthorized: input.approved ? true : null,
    },
    result,
    verdict,
    // Economics for the whole decision, verifier included — a cross-vendor
    // verify is real additional cost and hiding it would understate the true
    // price of a t3 request.
    usage: mergeUsage(usageOf(result), verifyUsage),
  };
}

/** Hand a human's answer to the runner that is blocked waiting for it.
 *
 * Deliberately routed THROUGH here rather than n8n calling the runner directly.
 * AGENTS.md states the invariant plainly — n8n calls the router, not the
 * runners — and an exception carved out for "just a delivery" is how a
 * chokepoint stops being one. The router also already holds RUNNER_TOKEN and
 * already speaks to the runners, so this adds no new trust relationship.
 */
function deliverAnswer(provider, payload) {
  const host = String(provider || "claude").includes("-runner")
    ? provider : `${provider || "claude"}-runner`;
  const data = JSON.stringify(payload);
  return new Promise((resolve, reject) => {
    const r = http.request(
      { host, port: 8080, path: "/ask/deliver", method: "POST",
        headers: { "content-type": "application/json",
                   "content-length": Buffer.byteLength(data),
                   "x-runner-token": RUNNER_TOKEN } },
      (resp) => {
        let b = "";
        resp.on("data", (c) => (b += c));
        resp.on("end", () => (resp.statusCode === 200
          ? resolve(b) : reject(new Error(`runner ${resp.statusCode}: ${b.slice(0, 200)}`))));
      });
    r.setTimeout(15000, () => r.destroy(new Error("runner timeout")));
    r.on("error", reject);
    r.end(data);
  });
}

const server = http.createServer((req, res) => {
  if (req.method === "GET" && req.url === "/health") return res.writeHead(200).end("ok");

  // Answer delivery for the ask-human gate. Separate path, same token as /route:
  // n8n already holds ROUTER_TOKEN, so this introduces no new secret.
  if (req.method === "POST" && req.url === "/ask/answer") {
    if (!tokenOk(req.headers["x-router-token"], ROUTER_TOKEN))
      return res.writeHead(401).end("unauthorized");
    let b = "";
    req.on("data", (c) => (b += c));
    req.on("end", async () => {
      const send = (code, obj) =>
        res.writeHead(code, { "content-type": "application/json" }).end(JSON.stringify(obj));
      let a;
      try { a = JSON.parse(b || "{}"); } catch { return send(400, { error: "bad json" }); }
      if (!a.ticket) return send(400, { error: "ticket is required" });
      try {
        await deliverAnswer(a.provider, {
          ticket: a.ticket, answered: a.answered, answer: a.answer,
          timedOut: a.timedOut, note: a.note,
        });
        return send(200, { delivered: a.ticket });
      } catch (e) {
        // Say that delivery failed. A silently dropped answer leaves the agent
        // reporting "nobody answered" when a human did — the exact confusion
        // this gate exists to prevent.
        return send(502, { error: `delivery failed: ${String(e.message || e)}`,
                           ticket: a.ticket });
      }
    });
    return;
  }

  if (req.method !== "POST" || req.url !== "/route") return res.writeHead(404).end("not found");
  if (!tokenOk(req.headers["x-router-token"], ROUTER_TOKEN))
    return res.writeHead(401).end("unauthorized");

  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", async () => {
    const send = (code, obj) =>
      res.writeHead(code, { "content-type": "application/json" }).end(JSON.stringify(obj));
    let input = {};
    try {
      input = JSON.parse(body || "{}");
      if (!input.prompt) return send(400, { error: "prompt is required" });
      const out = await route(input);
      await audit(input, out);
      send(200, out);
    } catch (e) {
      // Log policy rejections (unknown scope, PII block, …) too — they matter.
      await audit(input, { decision: { scope: input.scope || null }, blocked: "error:" + String(e.message || e) });
      send(500, { error: String(e.message || e) });
    }
  });
});

// Start the HTTP server ONLY when run directly (node server.js). When this file
// is require()'d by a unit test, we export the pure helpers instead of binding a
// port — no runtime behavior changes for the container entrypoint.
if (require.main === module) {
  server.listen(PORT, () => console.log(`router listening on :${PORT} (scopes: ${SCOPES ? "on" : "off"})`));
}

module.exports = {
  tokenOk,
  classifyRisk,
  resolveMode,
  resolveScope,
  checkConflicts,
  clampTier,
  resolveTier,
  piiScan,
  tryRules,
  auditRecord,
  lastHash,
  sha256,
};
