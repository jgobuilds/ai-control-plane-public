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
const actions = require("./actions.js");    // action binding: what a request may DO (see actions.js)

// Scope registry is optional — without it the router behaves as before.
let SCOPES = null;
try {
  SCOPES = JSON.parse(fs.readFileSync(process.env.SCOPES_PATH || "/app/context/scopes.json", "utf8"));
} catch { /* no scopes.json mounted — scope enforcement disabled */ }

const TIER_ORDER = ["t0", "t1", "t2", "t3"];
const idx = (t) => TIER_ORDER.indexOf(t);

// Which provider is the PRIMARY vendor — the one a blockCrossVendor scope may
// still use. Defined once because two copies of this rule is how a scope ends
// up refused on the dispatch path and escalated on the routing path, or worse,
// escalated to a tier the refusal then rejects. targetFor() and
// escalateForVendor() must agree by construction, not by review.
const isPrimaryVendor = (p) => String(p || "").startsWith("claude");

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
    // `action` is the verb the router GOVERNED by — identical to the old
    // `input.action ?? "advise"` under actionBinding:"warn", so existing readers
    // (eval_capture replays it) see no change. `declaredAction` is the caller's
    // claim and stays null when omitted — never back-filled — so the ledger can
    // show which lanes name nothing before enforcement is switched on.
    action: d.action ?? (d.risk?.binding ? actions.governingVerb(d.risk.binding) : null)
      ?? input.action ?? "advise",
    declaredAction: input.action ?? null,
    operation: d.risk?.binding?.operation?.name ?? null,
    actionBound: d.risk?.binding ? d.risk.binding.bound : null,
    actionUnderDeclared: d.risk?.binding ? d.risk.binding.underDeclared : null,
    // Only the NAMES of tools, and only for a bound request — what the runner was
    // actually allowed to use, which is the fact an incident review needs.
    toolsBound: d.risk?.binding?.bound && d.risk.binding.narrowing?.available
      ? d.risk.binding.narrowing.tools : null,
    trigger: input.trigger ?? "turn",
    approved: !!input.approved,
    requester: input.requester ?? null,
    approver: input.approver ?? null,
    approvalAuthorized: d.approvalAuthorized ?? null,
    blocked: out.blocked ?? null,
    tier: d.tier ?? null, provider: d.provider ?? null, model: d.model ?? null,
    // A vendor escalation moved this request to a more expensive tier without a
    // human asking. That is exactly the kind of automatic spend that has to be
    // attributable later — "why is this scope's notional cost double" needs an
    // answer in the ledger, not a reconstruction from policy.
    vendorEscalated: d.vendorEscalated ?? null,
    pii: d.pii?.hits ? { types: d.pii.hits.map((h) => h.type), total: d.pii.hits.reduce((a, h) => a + h.count, 0) } : null,
    // Guardrail findings — TYPES + count ONLY (never the matched prompt/output
    // substring), keeping the ledger metadata-only like the PII record above.
    guardrails: d.guardrails?.length ? { types: d.guardrails.map((f) => f.type), total: d.guardrails.length } : null,
    outputFindings: d.outputFindings?.length ? { types: d.outputFindings.map((f) => f.type), total: d.outputFindings.length } : null,
    verdict: out.verdict?.pass ?? null,
    // Without this the ledger cannot tell "passed", "no verify required", and
    // "the reviewer was unreachable" apart — all three land as verdict:null or
    // true, and only the third means the quality bar was not actually applied.
    // eval_metrics' verify-pass rate would silently improve during an outage.
    verifyStatus: d.verifyStatus ?? null,
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
  return rec;
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
  // A RETIRED scope accepts no work. Until now this held only by accident: the
  // generator emits no runner for a retired scope, so dispatch failed with
  // `getaddrinfo ENOTFOUND claude-runner-<scope>` — fail-closed, but for an
  // incidental reason and with an error that reads as a broken deployment. Leave
  // that container running once and a closed engagement quietly accepts work
  // again. Retirement is a policy state, so the policy layer enforces it.
  if (String(scope.node?.status || "").toLowerCase() === "retired") {
    const e = new Error(
      `scope "${scope.name}" is retired and accepts no work. Reopen it in the ` +
      `scope tree if that is wrong — do not route around it.`);
    e.statusCode = 410;   // Gone: it existed, deliberately no longer serves
    throw e;
  }
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
// Patterns + scanner live in the CANONICAL vendored module, so the router and
// both runners cannot disagree about what counts as PII. They previously
// could: this block was inline here and nowhere else, which is precisely how
// a runner accepts what the router refuses.
const { PII_PATTERNS, piiScan } = require("./pii-patterns.js");

// ---------- scope -> runner host (Phase 1 step 3) ----------
//
// gen_compose.py has written context/runner-map.json since phase 1 and the
// router never read it. Dispatch honoured `runnerHost` for `isolation: "silo"`
// scopes only; every POOL scope went to the SHARED runner with a cwd — which is
// exactly what "pool isolation is advisory (cwd only)" means. A cwd is a
// suggestion to a process that can read the whole mount.
//
// OFF BY DEFAULT. The per-root runners in compose.scopes.yml are not deployed,
// so enabling this before cutover routes every scope at a container that does
// not exist. Flip it in the same change window as the cutover.
const RUNNER_MAP_ON = String(process.env.RUNNER_MAP || "off").toLowerCase() === "on";
// Resolve the map relative to THIS FILE, not to an absolute container path.
// "/app/context/..." was correct in the image and nowhere else — it does not
// exist in a checkout, so CI would have loaded null and the flag-on path would
// have failed for a reason that had nothing to do with the code under test.
const RUNNER_MAP_CANDIDATES = [
  process.env.RUNNER_MAP_PATH,
  path.join(__dirname, "context", "runner-map.json"),        // in-image layout
  path.join(__dirname, "..", "context", "runner-map.json"),  // repo layout
].filter(Boolean);
let RUNNER_MAP = null, RUNNER_MAP_FROM = null;
for (const c of RUNNER_MAP_CANDIDATES) {
  try { RUNNER_MAP = JSON.parse(fs.readFileSync(c, "utf8")); RUNNER_MAP_FROM = c; break; }
  catch { /* try the next */ }
}

/** The dedicated runner for a SECOND-vendor provider, or null if none serves
 *  this scope.
 *
 *  gemini is pool-only by derivation, not by preference: `silo` and `pii:block`
 *  both create an isolation root AND put the risk model's data dimension at 3,
 *  which is where blockCrossVendor fires. So a root can never legitimately reach
 *  a second vendor, and giving one its own gemini container would be building an
 *  idle container to satisfy a case policy forbids.
 *
 *  Returning null is therefore "this scope has no second-vendor path", and the
 *  caller escalates to claude rather than refusing — a conflict-only root (a
 *  root whose pii is merely `warn`) is the case that reaches here, and refusing
 *  it would be another control that fails by looking like it worked.
 */
/** What to do when the reviewer could not be reached at all.
 *
 * Extracted so it can be tested. The live path is nearly unreachable by design:
 * the same-vendor fallback targets the SAME runner that just produced the
 * primary result, so if the work succeeded the fallback almost always succeeds
 * too — "both down" is a narrow race where the runner dies between two calls.
 * Nearly-unreachable is exactly the branch that rots untested, and one half of
 * it is a fail-closed risk control.
 *
 * Returns { status, blocked? }. `blocked` set means: do not return the work.
 */
function verifyOutcome({ required, crossError, fallbackError, fallbackTried }) {
  if (!crossError) return { status: "ok" };
  if (fallbackTried && !fallbackError) return { status: "degraded-same-vendor" };
  const why = `verifier unavailable (${String(crossError).slice(0, 160)})` +
    (fallbackError ? "; same-vendor fallback also failed" : "");
  // A verify the RISK MODEL demanded is a control. Returning unverified work
  // because the checker was down is the failure mode the control exists to
  // prevent, so this blocks. A verify triggered by the tier table is a quality
  // heuristic — it degrades, because destroying completed work to punish an
  // unrelated outage helps nobody.
  return required
    ? { status: "unavailable", blocked: "verify-unavailable", why }
    : { status: "unavailable", why };
}

function providerHostFor(scope, provider) {
  if (!RUNNER_MAP_ON || !RUNNER_MAP) return provider;   // pool mode: the shared runner
  const hosts = RUNNER_MAP.providerHosts || {};
  const host = hosts[String(provider)];
  if (!host) return null;
  // Pooled scopes only. `roots` is the same list the map already publishes, so
  // this cannot drift from the topology the generator emitted.
  const roots = new Set(RUNNER_MAP.roots || []);
  return roots.has(scope?.name) ? null : host;
}

function targetFor(scope, provider, opts = {}) {
  if (!RUNNER_MAP_ON || !scope)
    return scope?.controls?.isolation === "silo" ? scope.node.runnerHost : provider;
  if (!RUNNER_MAP || !RUNNER_MAP.scopeToHost)
    throw new Error("RUNNER_MAP=on but context/runner-map.json is missing. Refusing to " +
      "fall back to the shared runner — that fallback IS the bypass this map closes.");
  const host = RUNNER_MAP.scopeToHost[scope.name];
  if (!host)
    throw new Error(`scope "${scope.name}" has no runner-map entry; regenerate with ` +
      "scripts/gen_compose.py. Not falling back to a shared runner.");
  // A NON-CLAUDE provider defers to POLICY, which already decides this per
  // request via risk.controls.blockCrossVendor. An earlier version refused every
  // non-claude target on a mapped scope; that overrode a precise decision with a
  // cruder one and would have broken cross-provider verify for exactly the two
  // low-sensitivity scopes where policy deliberately permits it.
  //
  // It also closes a real hole. `blockCrossVendor` is documented as "don't send
  // confidential work to a 2nd vendor (addresses M4)" and was consulted ONLY in
  // the verify branch. A `pii:block` scope asking for a t1 task was dispatched to
  // gemini as its PRIMARY runner with the flag computed true and ignored —
  // verified against the running router. Here it is honoured on every path.
  if (provider && !isPrimaryVendor(provider)) {
    if (opts.crossVendorBlocked || !providerHostFor(scope, provider)) {
      // 403, not 500 — a policy refusal, not a fault. Retrying cannot help.
      //
      // This is now a BACKSTOP rather than the normal path: route() escalates a
      // blocked scope to the cheapest permitted tier before dispatching, so
      // reaching here means either no permitted tier exists above the resolved
      // one, or a caller reached dispatch without going through the escalation.
      // Refusing is still right in both cases — the alternative is sending
      // confidential work to the second vendor because a code path was missed.
      const e = new Error(
        `scope "${scope.name}" scores blockCrossVendor, so its work may not ` +
        `go to provider "${provider}". Policy computed this and it is enforced on the ` +
        "dispatch path, not only on the verifier. Routing normally escalates such a " +
        "request to the cheapest permitted tier instead of refusing; arriving here means " +
        "no permitted tier was available.");
      e.statusCode = 403;
      throw e;
    }
    // Policy permits vendor 2 AND this scope has a dedicated second-vendor
    // runner. That runner mounts exactly the pooled subtree — the same mounts as
    // the claude commons runner — so the scope isolation is structural on this
    // side too. It used to return the bare provider name, which resolved to the
    // shared `gemini-runner`: one container, whole workspace, no SCOPE_ROOT.
    return providerHostFor(scope, provider);
  }
  return host;
}

// ---------- runner dispatch ----------
/** Reduce a runner's error text to something safe to put in the audit ledger.
 *
 * THE LEDGER IS METADATA-ONLY. It stores promptSha256 and never the prompt —
 * that rule is what makes it safe to keep, and what the no-external-processor
 * position rests on. Propagating runner errors (finding D1) quietly broke it:
 * a failing CLI reports `Command failed: claude -p <THE ENTIRE PROMPT> --flags`,
 * that string became the Error message, and route()'s handler writes the message
 * into `blocked`. Two records in the live ledger carried a full prompt before
 * this existed.
 *
 * So: keep the first line only (the useful part is the command name and the
 * failure), drop everything from the `-p` flag onward, and cap hard. A truncated
 * diagnostic is a cost worth paying; a prompt in an append-only hash-chained
 * file cannot be taken back.
 */
function runnerErrorSummary(raw) {
  let s = String(raw == null ? "" : raw);
  s = s.split("\n")[0];                       // stderr tails are where prompts hide
  // `claude -p <prompt> --flags` / `gemini -p <prompt>`: cut at the prompt flag.
  s = s.replace(/(\s-p\s).*$/, "$1<prompt omitted>");
  return s.slice(0, 200);
}

// ---------- tool sets the router sends ----------
// Router-internal calls — triage and the verifier — judge text and act on
// nothing, and both are fed UNTRUSTED text: the raw prompt, the executor's
// output. Before this they were dispatched with no tool list at all, i.e. the
// runner's full default set including Bash, so a prompt injection in either
// landed in a session that could execute.
//
// They get NO tools — but only on a runner that can enforce "no tools". An
// earlier version of this sent `[]` everywhere, and the second-vendor runner
// refuses every tool list with a 422 until its read-only mode is configured —
// the empty list included. Triage runs on that vendor for every untyped request
// on a pooled scope, so under WARN, the mode that was meant to change nothing,
// untyped routing would have failed. Caught in cross-review before merge, not by
// a test: the fake runner in the unit tests accepted everything. Until
// risk.enforce.secondVendorReadOnly is true, internal calls to that vendor keep
// today's behaviour, and that residual is recorded in THREAT-MODEL H5.
function internalCallTools(provider, policy = POLICY) {
  return isPrimaryVendor(provider) || actions.secondVendorReadOnly(policy) ? [] : undefined;
}

// The executor's tool set. Bound (an operation, or actionBinding:"block"): the
// narrowed set, and a refusal if nothing could be narrowed — dispatching a bound
// request with an unbounded tool set would make the binding a label. Unbound
// (warn): exactly what the scope declared, as before.
function dispatchTools(binding, scope) {
  if (binding?.bound) {
    if (!binding.narrowing?.available) {
      const e = new Error(`action is bound (${actions.actionLabel(binding)}) but ${binding.narrowing?.reason || "no tool set could be derived"}. ` +
        "Refusing to dispatch it with an unbounded tool set.");
      e.statusCode = 500;
      throw e;
    }
    return binding.narrowing.tools.slice();
  }
  return scope?.controls?.allowedTools;
}

// Per-task tools for a fan-out. A caller may NARROW a task, never widen it: each
// task gets the intersection of what it asked for and what the router decided.
// When the router decided nothing (unbound, scope unrestricted) the caller's own
// narrowing stands, since it can only be tighter than unrestricted.
function fanoutTasks(tasks, decided) {
  return (tasks || []).map((t) => {
    const task = { ...(t || {}) };
    const asked = Array.isArray(task.allowedTools) ? task.allowedTools : null;
    if (Array.isArray(decided)) {
      task.allowedTools = asked ? asked.filter((x) => decided.includes(x)) : decided.slice();
    } else if (asked) {
      task.allowedTools = asked.slice();
    } else {
      delete task.allowedTools;
    }
    return task;
  });
}

function callRunner(hostOrProvider, payload, path = "/run") {
  // `path` defaults to /run so every existing call is unchanged. It was
  // hardcoded, so the /fanout endpoint would have posted fan-out tasks to
  // /run — silently, since the extra argument was simply ignored.
  // Accepts a provider name ("claude" -> claude-runner) or a full host
  // (a silo scope's dedicated runnerHost).
  const host = hostOrProvider.includes("-runner") ? hostOrProvider : `${hostOrProvider}-runner`;
  const data = JSON.stringify(payload);
  const opts = {
    host, port: 8080, path, method: "POST",
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
      resp.on("end", () => {
        let body;
        try { body = JSON.parse(b); } catch { body = { result: b }; }
        // A RUNNER ERROR IS AN ERROR. This resolved on any status, so a runner
        // 500 became a successful decision whose result was simply empty —
        // and the ledger recorded it as success. Found by the first live run of
        // a curated eval set: both vendor CLIs were failing (neither was logged
        // in), every /route returned 200, and the only visible symptom was an
        // empty string. "The model had nothing to say" and "the runner is
        // broken" have to be distinguishable, or nothing downstream can react
        // to the second.
        if (resp.statusCode >= 400) {
          const e = new Error(
            `runner ${host} returned ${resp.statusCode}: ` +
            runnerErrorSummary(body && body.error ? body.error : b));
          // Preserve a policy refusal's status (403/410/422 from the runner's
          // own scope/PII guards) so it stays distinguishable from a fault.
          e.statusCode = resp.statusCode < 500 ? resp.statusCode : 502;
          return reject(e);
        }
        resolve(body);
      });
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

async function triage(prompt, scope, triageOpts = {}) {
  // Triage runs BEFORE any tier is resolved, on the raw prompt, and its default
  // tier is t1 — the blocked vendor. Without escalating here a blockCrossVendor
  // scope failed at classification and never reached the routing that would
  // have escalated it, so the fix downstream would have been invisible.
  const { tier: triageTier } = escalateForVendor(POLICY.defaults.triageTier,
    { ...triageOpts, scope });
  const tier = POLICY.tiers[triageTier];
  const labels = Object.keys(POLICY.taskTypes).join(", ");
  // The RAW PROMPT went to a shared runner here regardless of scope, before any
  // routing had happened. Classification is still the tenant's content.
  const out = await callRunner(targetFor(scope, tier.provider, triageOpts), {
    prompt: `Classify this task as exactly one of: ${labels}. Reply with only the label.\n\nTASK:\n${prompt}`,
    model: tier.model,
    allowedTools: internalCallTools(tier.provider),
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

/** Cheapest PERMITTED tier for a scope that may not use a second vendor.
 *
 * `blockCrossVendor` used to be terminal: a `pii:block` scope asking for a t1
 * task type got a 403 and nothing else. That reads as a working control and is
 * actually an outage — classify, extract and route are t1, so the scopes doing
 * confidential client work lost the cheap tier entirely rather than paying more
 * for it. A refusal that looks like policy succeeding is the expensive kind of
 * bug.
 *
 * So: escalate UPWARD to the cheapest tier the vendor rule permits. Upward
 * only. Searching the whole table for "cheapest permitted" could land BELOW the
 * resolved tier and quietly answer a hard question with a weaker model — the
 * cost shows up in the ledger, but a capability downgrade would not.
 *
 * The caller records `vendorEscalated` on the decision, because escalation
 * spends real money and an unlogged spend decision is one nobody made.
 */
function escalateForVendor(tierId, { crossVendorBlocked, scope }) {
  const none = { tier: tierId, escalatedFrom: null };
  const t = POLICY.tiers[tierId];
  if (!t || t.kind !== "model" || isPrimaryVendor(t.provider)) return none;
  // Two reasons a second vendor is off the table, and both must escalate rather
  // than refuse. The first is policy (confidential data). The second is
  // topology: no second-vendor runner serves this scope, which is the case for
  // an isolation root — and targetFor tests the SAME predicate, so the two
  // cannot disagree about whether a dispatch would have been allowed.
  if (!crossVendorBlocked && providerHostFor(scope, t.provider)) return none;
  const next = TIER_ORDER.slice(idx(tierId) + 1).find((id) => {
    const c = POLICY.tiers[id];
    return c && c.kind === "model" && isPrimaryVendor(c.provider);
  });
  // No permitted tier above this one — fall through unchanged and let
  // targetFor's 403 refuse. Escalation is the preferred outcome, not a
  // guarantee, and silently dispatching to the blocked vendor is not an option.
  return next ? { tier: next, escalatedFrom: tierId } : none;
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
//
// ACCESS COMES FROM THE BINDING, NOT FROM THE CALLER. `access` used to be read
// straight off `input.action` — a verb the caller supplies, defaulting to the
// least risky one — so a request that named nothing was scored as "advise" while
// its agent held Bash. actions.bindAction() now decides the governing verb: a
// named OPERATION always sets it (and may be raised, never lowered), and a bare
// verb governs only as far as risk.enforce.actionBinding allows. Under "warn" the
// governing verb is exactly the legacy one, so live scoring does not move until
// enforcement is chosen; the binding rides along on `risk.binding` either way.
function classifyRisk(input, scope, policy = POLICY) {
  const R = policy.risk;
  if (!R) return null;
  const binding = actions.bindAction(input, scope, policy);   // throws on an undeclared operation
  const verb = actions.governingVerb(binding);
  const ctl = scope?.controls || {};
  const dataKey = ctl.isolation === "silo" ? "silo" : (ctl.pii || "default");
  const dims = {
    data: R.data[dataKey] ?? R.data.default,
    access: R.access[verb] ?? R.access.default,               // advise|write|apply|send|…
    autonomy: R.autonomy[input.trigger] ?? R.autonomy.default, // turn|scheduled|proactive
  };
  const base = Math.max(dims.data, dims.access, dims.autonomy);
  const highs = Object.values(dims).filter((v) => v >= 3).length;
  let levelIdx = Math.max(0, Math.min(3, highs >= 2 ? 3 : base - 1));
  const on = (spec) => !!spec && Object.entries(spec).some(([d, thr]) => dims[d] >= thr);
  const cbd = R.controlsByDimension || {};
  // AN IRREVERSIBLE OPERATION ALWAYS NEEDS A HUMAN ("Human gates on irreversible
  // acts" — AGENTS.md), whatever its verb scores. And at a level where segregation
  // of duties applies, so the person who asked for the invoice is not the person
  // who approves it. A write-class operation alone would reach "medium", where
  // the requester may approve their own work.
  const irreversible = !!binding.operation?.irreversible;
  if (irreversible) levelIdx = Math.max(levelIdx, R.levels.indexOf("high") >= 0 ? R.levels.indexOf("high") : 2);
  return {
    level: R.levels[levelIdx], dims, binding,
    controls: {
      requireVerify: on(cbd.requireVerify),
      requireApproval: on(cbd.requireApproval) || irreversible,
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
// ---------- action gates (shared by /route and /fanout) ----------
// Risk + proportional controls, approver RBAC, and the operating-mode cap, in
// that order. Every gate reads the SAME governing verb from the action binding,
// so the risk score and the mode cap cannot disagree about what the request does
// — a cap keyed off the caller's verb would let "advise" through a scope whose
// operation sends, which is the same bypass one gate further on.
//
// Returns { risk, mode, modeStamp, blocked } where `blocked` is the response to
// return, or null to continue.
function modeForbids(mode, verb, policy = POLICY) {
  const ranks = actions.verbRanks(policy);
  const rank = (v) => ranks[v] ?? ACTION_RANK[v];
  return (rank(verb) ?? 1) > (rank(mode.maxAction) ?? 3);
}

function actionGates(input, scope, pii, extra = {}) {
  const risk = classifyRisk(input, scope);
  const binding = risk?.binding || null;
  const verb = binding ? actions.governingVerb(binding) : (input.action || "advise");
  const label = binding ? actions.actionLabel(binding) : verb;
  const actionStamp = {
    action: verb, declaredAction: input.action ?? null,
    ...(binding ? { actionBinding: actions.summarize(binding) } : {}),
  };
  const base = { scope: scope?.name || null, ...extra };

  if (risk) {
    if (risk.controls.requireIsolation && scope?.controls?.isolation !== "silo") {
      if (POLICY.risk.enforce?.requireIsolation === "block")
        throw new Error(`risk=${risk.level}: high-sensitivity data must run in an isolation:"silo" scope; "${scope?.name}" is not.`);
      risk.isolationWarning = `scope "${scope?.name}" handles high-sensitivity data but isn't isolation:"silo" (enforce=warn)`;
    }
    // A human must approve before an acting/self-triggered/irreversible request runs.
    if (risk.controls.requireApproval && !input.approved) {
      return { risk, blocked: {
        decision: { ...base, risk, ...actionStamp, trigger: input.trigger || "turn", pii },
        blocked: "approval-required",
        message: `risk=${risk.level} (action=${label}, autonomy=${input.trigger || "turn"}). Route through the approval gate, then re-call with approved:true.`,
      } };
    }
  }

  // Approver RBAC + segregation of duties (M5): the named approver must be in an
  // identityGroup authorized to approve at this risk level/scope, and
  // (high/critical) must differ from the requester.
  if (input.approved) {
    const authz = rbac.authorizedToApprove({
      requester: input.requester,
      approver: input.approver,
      approverGroups: input.approverGroups,
      scope: scope?.name || null,
      riskLevel: risk?.level || null,
    }, POLICY);
    if (!authz.ok) {
      return { risk, blocked: {
        decision: {
          ...base, risk, ...actionStamp, trigger: input.trigger || "turn",
          requester: input.requester || null, approver: input.approver || null,
          approvalAuthorized: false, pii,
        },
        blocked: "approval-unauthorized",
        message: `approval rejected: ${authz.reason}`,
      } };
    }
  }

  // Operating mode — cap what the agent may DO, and record the human's role.
  const mode = resolveMode(input, scope);
  if (mode && modeForbids(mode, verb)) {
    return { risk, mode, blocked: {
      decision: { ...base, mode: mode.name, risk, ...actionStamp },
      blocked: "mode-forbids-action",
      message: `operating mode "${mode.name}" caps the agent at "${mode.maxAction}"; ` +
        `${label} must be performed by a human, not the agent.`,
    } };
  }
  // Stamps the workflow acts on: is the output advisory (human is the actor), or
  // must it pass human validation before it's authoritative (AI-led support)?
  const modeStamp = mode && {
    mode: mode.name,
    advisory: mode.humanValidation === "actor",
    requiresOutputValidation: mode.humanValidation === "output",
  };
  return { risk, mode, modeStamp, actionStamp, blocked: null };
}

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

  // 0b–0c. risk, approval, approver RBAC and the operating-mode cap. One
  // implementation shared with /fanout (actionGates, below): a gate that exists on
  // one entrance and not the other is not a gate, it is a choice of door.
  const gates = actionGates(input, scope, pii);
  if (gates.blocked) return gates.blocked;
  const { risk, modeStamp, actionStamp } = gates;

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
      const t = await triage(input.prompt || "", scope,
        { crossVendorBlocked: !!risk?.controls?.blockCrossVendor });
      if (t) { task_type = t; viaTriage = true; resolved = resolveTier({ task_type, requested: input }); }
    }
    if (!resolved) resolved = { tier: POLICY.defaults.fallbackTier, why: "fallback" };
  }

  // 3. clamp to cost ceiling, then honour the vendor rule by ESCALATING rather
  // than refusing. Order matters: clamp first (the ceiling is about money),
  // escalate second (the vendor rule is about data). Escalating first would let
  // the clamp drop the request straight back onto the blocked vendor.
  const { tier: clampedTier, clamped } = clampTier(resolved.tier, { allowFrontier: !!input.allowFrontier });
  // Bound work holding anything above read-class needs a runner that can enforce
  // its tool set; the second-vendor runner can only offer read-only plan mode.
  // Treated exactly like the data rule, and computed ONCE so escalateForVendor and
  // targetFor read the same answer — they must agree by construction.
  const enforcingVendor = actions.requiresEnforcingVendor(risk?.binding, POLICY);
  const vendorLocked = !!risk?.controls?.blockCrossVendor || enforcingVendor;
  const { tier: tierId, escalatedFrom } = escalateForVendor(clampedTier,
    { crossVendorBlocked: vendorLocked, scope });
  const vendorEscalated = escalatedFrom && {
    from: escalatedFrom, to: tierId,
    fromProvider: POLICY.tiers[escalatedFrom]?.provider || null,
    // Distinguishing the two is the point of logging it at all. "policy" means
    // the data class forbids a second vendor; "no-runner" means this scope has
    // no second-vendor container, which is a TOPOLOGY fact and the thing you
    // would want to see if someone expected cheap-tier traffic and got billed
    // for claude instead.
    why: risk?.controls?.blockCrossVendor ? "blockCrossVendor"
      : enforcingVendor ? "tool-binding-needs-enforcing-runner" : "no-second-vendor-runner",
  };
  const tier = POLICY.tiers[tierId];

  const allowOverride = POLICY.controls.allowRequestOverride;
  const provider = (allowOverride && input.provider) || tier.provider;
  const model = (allowOverride && input.model) || tier.model;

  // 4. dispatch — silo scopes go to their dedicated runner; pool scopes get a
  // cwd inside the shared runner's workspace.
  const target = targetFor(scope, provider, { crossVendorBlocked: vendorLocked });
  // The tool set the runner receives is chosen HERE, from the action binding —
  // not passed through from the scope unexamined. The old comment said unlisted
  // tools were "effectively denied"; a live probe (2026-09-17, claude 2.1.220)
  // showed `--allowedTools` restricts nothing under --dangerously-skip-permissions,
  // so that was never true. Enforcement is the runner's `--tools` /
  // `--disallowedTools`; what this line owns is that the list is the decided one.
  const result = await callRunner(target, {
    prompt: input.prompt, model, maxTurns: input.maxTurns,
    cwd: scope && scope.controls.isolation !== "silo" ? scope.cwd : undefined,
    allowedTools: dispatchTools(risk?.binding, scope),
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
  let verifyStatus = "none";
  // MANDATORY vs HEURISTIC, and the distinction is the whole fix. A verify
  // demanded by the risk model (`requireVerify`) or asked for explicitly is a
  // CONTROL: if it cannot run, the safe outcome is to block. A verify triggered
  // by the tier or task-type table is a QUALITY heuristic: if it cannot run, the
  // safe outcome is to return the work and say the verdict is missing.
  //
  // They used to fail identically, and identically badly. gemini being
  // unauthenticated took down every t3 request in every non-confidential scope —
  // the primary work ran on claude, cost real tokens, and was then discarded
  // because the SECOND vendor was unreachable. Losing the reviewer should
  // degrade the answer, not destroy it.
  const verifyRequired = !!(input.verify || risk?.controls?.requireVerify);
  if (verifyRequired || ttVerify || tierVerify) {
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
    // The WORK goes here. On a mapped scope this refuses rather than shipping
    // tenant output to a shared runner.
    const runVerify = async (vendor) => {
      const vr = await callRunner(targetFor(scope, vendor,
        { crossVendorBlocked: !!risk?.controls?.blockCrossVendor }),
        { prompt: reviewPrompt, model: (vendor === other ? otherTier : POLICY.tiers[tierId])?.model,
          allowedTools: internalCallTools(vendor) });
      verifyUsage = mergeUsage(verifyUsage, usageOf(vr));
      const m = String(textOf(vr) || "").match(/\{[\s\S]*\}/);
      const v = m ? safeParse(m[0]) : { pass: null, reasons: ["unparseable verdict"] };
      return { ...v, verifiedBy: vendor };
    };

    let crossError = null, fallbackError = null, fallbackTried = false;
    try {
      verdict = await runVerify(other);
    } catch (e) {
      crossError = e.message || e;
      // The reviewer's vendor is unreachable. Try the SAME vendor once before
      // giving up: most of a fresh-context reviewer's value is the fresh
      // context, not the second brand. Cross-vendor is what makes it
      // independent; a same-vendor review is weaker but far from nothing, and
      // it is recorded as `degraded` so nobody reads it as the independent one.
      if (provider !== other) {
        fallbackTried = true;
        try { verdict = await runVerify(provider); }
        catch (e2) { fallbackError = e2.message || e2; }
      }
    }
    const outcome = verifyOutcome({ required: verifyRequired, crossError, fallbackError, fallbackTried });
    verifyStatus = outcome.status;
    if (outcome.blocked) {
      return {
        decision: { tier: tierId, provider, model, scope: scope?.name || null,
                    risk, verifyStatus, ...modeStamp },
        blocked: outcome.blocked,
        message: `verification is REQUIRED for this request and ${outcome.why}. Refusing ` +
          "to return unverified work when the risk model demanded a check.",
      };
    }
    if (outcome.status === "unavailable") {
      verdict = { pass: null, reasons: [outcome.why], verifiedBy: null, unavailable: true };
    }
  }

  return {
    decision: {
      tier: tierId, provider, model, task_type: task_type || null, viaTriage, clamped,
      why: resolved.why, scope: scope?.name || null, scopeCwd: scope?.cwd || null,
      ...(vendorEscalated ? { vendorEscalated } : {}),
      isolation: scope?.controls.isolation || null, pii, risk, ...modeStamp, ...guardStamp, ...actionStamp,
      // "no verify ran" and "the verifier could not be reached" are different
      // facts. verdict:null alone conflates them, and the second is the one an
      // operator needs to see.
      verifyStatus,
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


// ---------- /fanout: the ungoverned path, governed (ISOLATION-FIX-PLAN step 4) ----------
//
// Three workflows called http://claude-runner:8080/fanout DIRECTLY. That path
// got no scope resolution, no kill switch, no circuit breaker, no risk tiering
// — and wrote NO AUDIT RECORD, against this repo's own constraint that no path
// may act without leaving one. "n8n calls the router, not the runners" was the
// doctrine; fan-out was the exception nobody had written down.
//
// Fan-out is claude-only: it needs git worktrees, which only that runner has.
// So there is no provider choice to make here, only a SCOPE one — which is
// exactly what was missing.
async function fanoutRoute(input) {
  const scopeName = (SCOPES ? (input.scope || SCOPES.defaultScope) : input.scope) || null;

  // Same gate order as /route: halt and breaker BEFORE any work, so an operator
  // halt stops a fan-out that would otherwise start N agents in parallel.
  let scopeChain = scopeName ? [scopeName] : [];
  if (SCOPES && scopeName) {
    try { scopeChain = resolveScope(scopeName).chain; } catch { /* bare-name fallback */ }
  }
  const halt = killswitch.isHalted(killswitch.haltState(process.env.CONTROL_DIR), scopeChain);
  if (halt.halted)
    return { decision: { scope: scopeName, endpoint: "fanout" }, blocked: "halted", reason: halt.reason };
  const brk = killswitch.breaker(POLICY, AUDIT_LOG, scopeName);
  if (brk.tripped)
    return { decision: { scope: scopeName, endpoint: "fanout" }, blocked: "circuit-open", reason: brk.reason };

  let scope = null, pii = null;
  if (SCOPES) {
    scope = resolveScope(scopeName);
    checkConflicts(scope);
    // EVERY task's prompt. A fan-out is N prompts; scanning one is theatre.
    const hits = [];
    for (const t of (input.tasks || [])) hits.push(...piiScan(t && t.prompt));
    if (hits.length) {
      pii = { hits, policy: scope.controls.pii, allowPii: !!input.allowPii };
      if (scope.controls.pii === "block" && !input.allowPii)
        throw new Error(
          `PII detected in a fan-out task (${hits.map((h) => h.type).join(", ")}) and scope ` +
          `"${scope.name}" blocks raw PII. Scrub/tokenize it, or use the approved exception lane.`);
    }
  }

  // SAME GATES AS /route. Before this, /fanout ran no risk classification and no
  // mode cap at all, and passed each task's allowedTools through as the caller
  // wrote them — so it was the way around everything /route decides, including a
  // named operation's approval. A fan-out is N agents; it gets one decision that
  // binds all of them.
  const gates = actionGates(input, scope, pii, { endpoint: "fanout" });
  if (gates.blocked) return gates.blocked;
  const { risk, modeStamp, actionStamp } = gates;
  const decided = dispatchTools(risk?.binding, scope);

  const target = targetFor(scope, "claude");
  const result = await callRunner(target, {
    repo: input.repo, base: input.base, tasks: fanoutTasks(input.tasks, decided), clean: input.clean,
  }, "/fanout");
  return {
    decision: {
      scope: scope?.name || null, endpoint: "fanout", target,
      tasks: (input.tasks || []).length, pii, risk, ...modeStamp, ...actionStamp,
      // Notional only, like every other cost here — these runners bill against a
      // subscription. See AIOPS.md.
      deterministic: true, tier: "n/a",
    },
    result,
  };
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

  if (req.method !== "POST" || !["/route", "/fanout"].includes(req.url))
    return res.writeHead(404).end("not found");
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
      const isFanout = req.url === "/fanout";
      if (isFanout) {
        if (!Array.isArray(input.tasks) || !input.tasks.length)
          return send(400, { error: "tasks[] is required" });
      } else if (!input.prompt) {
        return send(400, { error: "prompt is required" });
      }
      const out = isFanout ? await fanoutRoute(input) : await route(input);
      await audit(input, out);
      send(200, out);
    } catch (e) {
      // Log policy rejections (unknown scope, PII block, …) too — they matter.
      await audit(input, { decision: { scope: input.scope || null }, blocked: "error:" + String(e.message || e) });
      // Honour a status the guard chose. This was fixed in the RUNNERS and left
      // wrong here, so a policy refusal — a retired scope, a blocked vendor —
      // arrived as 500 and read as "the router broke". Only one of those is
      // worth retrying, and a client that retries a refusal turns one refusal
      // into a loop.
      send(e.statusCode || 500, { error: String(e.message || e) });
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
  route,
  actionGates,
  modeForbids,
  dispatchTools,
  fanoutTasks,
  internalCallTools,
  classifyRisk,
  resolveMode,
  resolveScope,
  checkConflicts,
  clampTier,
  escalateForVendor,
  verifyOutcome,
  runnerErrorSummary,
  resolveTier,
  piiScan,
  targetFor,
  fanoutRoute,
  tryRules,
  auditRecord,
  lastHash,
  sha256,
};
