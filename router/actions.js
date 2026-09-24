// Action binding: what a request may DO is decided by the control plane, not by
// the caller's description of itself.
//
// WHY THIS EXISTS. Risk (classifyRisk) and the operating-mode cap both used to
// read `input.action` — a verb the CALLER supplies, defaulting to "advise", the
// least risky one. A request that omitted it, or said "advise" while the agent
// held Bash, skipped requireVerify and requireApproval entirely. Of the five
// /route call sites in n8n-workflows/, only research-watch declares a verb (`advise`,
// correctly — its prompt only asks for advice); both incident-responder calls and
// the router-dispatch example omit it, and dispatch forwards whatever its own
// caller sent (counted 2026-09-17). This repo
// already holds the rule that answers it — "a caller-declared policy would hand
// the bypass back" (ISOLATION-FIX-PLAN.md, about PII policy) — it just had not
// been applied to the verb.
//
// TWO LAYERS, because the risk that matters to a business is rarely a verb:
//
//   OPERATIONS  named, per scope: `invoice-customer` declares its access class,
//               whether it is irreversible, and the tools it may use. A request
//               naming an operation is scored by what the OPERATION does, can
//               never declare itself below that, and an irreversible operation
//               always needs a human — at a risk level where segregation of
//               duties applies, so the requester cannot approve their own
//               invoice. Naming an operation the scope does not declare is
//               refused. Always binding: nothing called /route with `operation`
//               before this, so there is no legacy traffic to break.
//
//   VERBS       the generic fallback. The effective verb narrows the TOOL SET the
//               runner receives, so a declared `advise` cannot write because it
//               has nothing to write with — the declaration becomes true instead
//               of trusted. Governed by `risk.enforce.actionBinding`. Only the
//               literal "warn" relaxes it: warn records what binding WOULD do and
//               changes nothing, so the live dispatcher is not silently cut to
//               read-only. Anything else — "block", a typo, a missing key —
//               enforces, because a misspelt security switch must not quietly
//               turn the control off. policy.json states the value explicitly:
//               "block" since 2026-09-18, after a static audit of every caller
//               (its _actionBinding_note records the evidence and the rollback).
//
// ENFORCEMENT IS THE RUNNER'S HALF. Narrowing produces a tool list; making that
// list restrict anything is the runner's job. Probed 2026-09-17 (claude 2.1.220):
// `--allowedTools Read` with --dangerously-skip-permissions still ran Bash;
// `--tools Read` did not. claude-runner now enforces the list with `--tools`
// (built-ins) and `--disallowedTools mcp__<server>` (MCP), and
// scripts/probe_cli_enforcement.py re-proves that against each CLI build before
// it is promoted. The operation gates (class, irreversible -> approval, mode cap)
// are enforced here regardless.

"use strict";

const VERBS_EXCLUDED = new Set(["default", "_comment"]);

function statusError(msg, statusCode) {
  const e = new Error(msg);
  e.statusCode = statusCode;
  return e;
}

// Verb -> rank, from policy.risk.access. The ONE source for ranking; server.js's
// ACTION_RANK duplicated it by hand, which is how the two could drift.
function verbRanks(policy) {
  const A = policy?.risk?.access || {};
  const out = {};
  for (const [k, v] of Object.entries(A)) if (!VERBS_EXCLUDED.has(k) && typeof v === "number") out[k] = v;
  return out;
}

function isKnownVerb(verb, policy) {
  return typeof verb === "string" && Object.prototype.hasOwnProperty.call(verbRanks(policy), verb);
}

// The highest-ranked verb — what an unbounded session can actually do.
function topVerb(policy) {
  const ranks = verbRanks(policy);
  return Object.keys(ranks).reduce((a, b) => (ranks[b] > (ranks[a] ?? -1) ? b : a), null);
}

// ---------- operations ----------

// Validate one declared operation against policy. Returns a list of problems;
// used both at request time (fail the request) and by the conformance test (fail
// CI), so a malformed declaration is caught before anything names it.
function operationProblems(name, op, policy) {
  const p = [];
  if (!op || typeof op !== "object") return [`operation "${name}" must be an object`];
  if (!isKnownVerb(op.access, policy))
    p.push(`operation "${name}" has access "${op.access}", which is not a verb in policy.risk.access`);
  if (op.irreversible !== undefined && typeof op.irreversible !== "boolean")
    p.push(`operation "${name}": irreversible must be true or false`);
  if (op.tools !== undefined) {
    if (!Array.isArray(op.tools) || op.tools.some((t) => typeof t !== "string" || !t))
      p.push(`operation "${name}": tools must be an array of tool names`);
    else {
      for (const t of op.tools) {
        const why = enforceableName(t);
        if (why) p.push(`operation "${name}": ${why}`);
      }
    }
    if (Array.isArray(op.tools) && isKnownVerb(op.access, policy)) {
      // An operation that claims `read` but lists Bash is describing itself below
      // what it can do — the exact lie this module exists to stop.
      const ranks = verbRanks(policy);
      for (const t of op.tools) {
        const cls = toolClass(t, policy);
        if (cls.known && ranks[cls.verb] > ranks[op.access])
          p.push(`operation "${name}" declares access "${op.access}" but tool "${t}" is "${cls.verb}"`);
      }
    }
  }
  return p;
}

function resolveOperation(input, scope, policy) {
  if (input.operation === undefined || input.operation === null || input.operation === "") return null;
  if (typeof input.operation !== "string") throw statusError("operation must be a string", 400);
  const ops = scope?.controls?.operations || {};
  const op = Object.prototype.hasOwnProperty.call(ops, input.operation) ? ops[input.operation] : undefined;
  if (!op) {
    // Refused, not defaulted. An agent that names an operation the business never
    // declared is either mistaken or probing; neither should get a guessed class.
    throw statusError(
      `operation "${input.operation}" is not declared on scope "${scope?.name ?? "(none)"}". ` +
      `Declare it under the scope's controls.operations before anything may perform it.`, 403);
  }
  const problems = operationProblems(input.operation, op, policy);
  if (problems.length) throw statusError(`misconfigured operation: ${problems.join("; ")}`, 500);
  return { name: input.operation, access: op.access, irreversible: op.irreversible === true,
           tools: Array.isArray(op.tools) ? op.tools.slice() : null };
}

// ---------- tools ----------

// Shape, agreed with the runner side (policy.json is owned there):
//   risk.toolAccess = { unlisted: "execute",
//                       tools: { Read: "read", Bash: "execute", "mcp__n8n": "execute", ... } }
//
// MCP IS GOVERNED PER SERVER, NOT PER TOOL. Probed 2026-09-17 (claude 2.1.220):
// `--tools` does not restrict MCP tools at all; `--disallowedTools mcp__<server>`
// removes a whole server, even under --dangerously-skip-permissions. There is no
// flag that keeps one tool of a server and drops another, so the runner rejects
// tool-level MCP names (422) rather than pretend. The consequence for a business
// operation is real and stated here rather than discovered later: an operation
// that needs `mcp__quickbooks` gets EVERY tool that server exposes — reading,
// creating, sending, voiding — so its access class must be the class of the
// whole server, not of the one call it means to make.

function baseName(tool) {
  // "Bash(git log:*)" -> "Bash". Specifiers narrow nothing under skip-permissions
  // and the runner rejects them; classifying by base tool is the same assumption.
  return String(tool).replace(/\(.*$/, "");
}

// "mcp__quickbooks__create_invoice" -> "mcp__quickbooks". Null for non-MCP names.
function mcpServer(tool) {
  const m = /^mcp__([^_](?:[^_]|_(?!_))*)(?:__|$)/.exec(String(tool));
  return m ? `mcp__${m[1]}` : null;
}

// A tool's verb class: exact name, base name, then its MCP server, then
// `unlisted`. Unlisted defaults to the TOP verb — a tool nobody classified is
// treated as able to do anything, so it is only ever handed to a request that is
// already allowed everything.
function toolClass(tool, policy) {
  const TA = policy?.risk?.toolAccess;
  const T = (TA && typeof TA.tools === "object" && TA.tools) || {};
  const fallback = isKnownVerb(TA?.unlisted, policy) ? TA.unlisted : topVerb(policy);
  for (const k of [tool, baseName(tool), mcpServer(tool)]) {
    if (k && typeof T[k] === "string") return { verb: T[k], known: true };
  }
  return { verb: fallback, known: false };
}

// Names the runner can actually enforce: a bare built-in, or a whole MCP server.
function enforceableName(tool) {
  if (/[()]/.test(tool)) return `"${tool}" is a specifier — specifiers narrow nothing under skip-permissions`;
  const srv = mcpServer(tool);
  if (srv && srv !== tool) return `"${tool}" names one MCP tool — only whole servers are enforceable; use "${srv}"`;
  return null;
}

// The tool set a request should run with.
//   base     the scope's declared allowedTools, or — when it declares none, which
//            is the runner's unrestricted default — every tool the policy
//            classifies by exact name
//   op       an operation that lists tools narrows to exactly those
//   verb     keep only tools whose class is at or below the effective verb
function narrowTools({ effective, scope, operation, policy }) {
  const TA = policy?.risk?.toolAccess;
  if (!TA || typeof TA.tools !== "object")
    return { available: false, reason: "policy.risk.toolAccess.tools is not defined — nothing to narrow against" };
  const ranks = verbRanks(policy);
  const cap = ranks[effective];
  const declared = Array.isArray(scope?.controls?.allowedTools)
    ? scope.controls.allowedTools.filter((t) => typeof t === "string" && t.trim()).map((t) => t.trim())
    : [];
  let candidates = declared.length
    ? declared
    : Object.keys(TA.tools);
  if (operation?.tools) {
    const want = new Set(operation.tools);
    // A scope that declares its tools also bounds what an operation may use: the
    // operation can pick from the scope's set, never add to it.
    candidates = declared.length
      ? candidates.filter((t) => want.has(t) || want.has(baseName(t)))
      : operation.tools.slice();
  }
  const tools = [], dropped = [];
  for (const t of candidates) (ranks[toolClass(t, policy).verb] <= cap ? tools : dropped).push(t);
  return { available: true, tools, dropped };
}

// ---------- the binding ----------

// Resolve what this request is actually allowed to do.
//   declared    what the caller said (may be absent or unknown)
//   effective   what it is scored and capped as
//   bound       whether `effective` governs risk/mode/tools right now
// Operations always bind. Verbs bind only under actionBinding:"block"; under
// "warn" the legacy verb governs and the binding is recorded beside it.
function bindAction(input, scope, policy) {
  const enforce = policy?.risk?.enforce?.actionBinding === "warn" ? "warn" : "block";
  const ranks = verbRanks(policy);
  const declaredRaw = input.action;
  const declaredKnown = isKnownVerb(declaredRaw, policy);
  const operation = resolveOperation(input, scope, policy);

  // The legacy reading, byte-for-byte what server.js did before: unknown or
  // omitted -> "advise" for the mode cap and policy.risk.access.default for risk.
  const legacy = declaredKnown ? declaredRaw : "advise";

  const notes = [];
  if (declaredRaw !== undefined && declaredRaw !== null && !declaredKnown)
    notes.push(`unknown action "${declaredRaw}"`);

  // `trigger` is caller-declared too, and had the same hole: an unknown value
  // fell to autonomy.default (1, a human turn), so research-watch's "schedule"
  // (the key is "scheduled") skipped requireVerify. Omitted keeps meaning a human
  // turn. A value the policy does not define is recorded under warn and refused
  // under block, the same treatment an unknown verb gets.
  const triggers = Object.keys(policy?.risk?.autonomy || {}).filter((k) => !VERBS_EXCLUDED.has(k) && !k.startsWith("_"));
  const trig = input.trigger;
  if (trig !== undefined && trig !== null && !triggers.includes(trig)) {
    if (enforce === "block")
      throw statusError(`unknown trigger "${trig}" — one of ${triggers.join(", ")} (or omit it for a human-initiated turn)`, 400);
    notes.push(`unknown trigger "${trig}"`);
  }

  let effective, bound;
  if (operation) {
    // Declared verb may RAISE an operation's class, never lower it.
    effective = declaredKnown && ranks[declaredRaw] > ranks[operation.access] ? declaredRaw : operation.access;
    bound = true;
    if (declaredKnown && ranks[declaredRaw] < ranks[operation.access])
      notes.push(`declared "${declaredRaw}" is below operation "${operation.name}" ("${operation.access}")`);
  } else if (enforce === "block") {
    // Blocking mode: an omitted or unknown verb is not a free "advise". It has to
    // be named, because the tool set is cut to match it.
    if (!declaredKnown) {
      throw statusError(
        declaredRaw === undefined || declaredRaw === null
          ? `action is required (risk.enforce.actionBinding is "block"): name what this request will do — one of ${Object.keys(ranks).join(", ")}`
          : `unknown action "${declaredRaw}" — one of ${Object.keys(ranks).join(", ")}`, 400);
    }
    effective = declaredRaw;
    bound = true;
  } else {
    effective = legacy;
    bound = false;
  }

  const narrowing = narrowTools({ effective, scope, operation, policy });
  return {
    enforce, bound,
    declared: declaredRaw ?? null,
    legacy,
    effective,
    operation,
    underDeclared: !!(operation && declaredKnown && ranks[declaredRaw] < ranks[operation.access]),
    notes,
    narrowing,
  };
}

// The verb that risk scoring and the mode cap should read.
function governingVerb(binding) {
  return binding.bound ? binding.effective : binding.legacy;
}

// Can the second-vendor runner enforce a read-only tool list? Only when policy
// says so with the literal `true`. Its read-only mode needs READONLY_ARGS, which
// ships unset and has never been probed against a live session, and with it unset
// the runner refuses EVERY tool list — including the empty one — with a 422. A
// missing or misspelt key therefore means "no", which keeps work on the primary
// vendor: more expensive, never less restricted.
function secondVendorReadOnly(policy) {
  return policy?.risk?.enforce?.secondVendorReadOnly === true;
}

// Does this request need a runner that can actually enforce its tool set?
// The claude runner can (built-ins via --tools, MCP per server). The second-vendor
// runner can at most offer read-only, and only once secondVendorReadOnly is on:
// until then ALL bound work is kept off it, because a read-only list would be
// refused there too. When it is on, only bound work above read-class is kept off.
// Unbound traffic returns false, so today's cheap-tier routing is unchanged under
// warn.
function requiresEnforcingVendor(binding, policy) {
  if (!binding?.bound) return false;
  if (!secondVendorReadOnly(policy) || !binding.narrowing?.available) return true;
  const ranks = verbRanks(policy);
  const readRank = ranks.read ?? 1;
  return binding.narrowing.tools.some((t) => (ranks[toolClass(t, policy).verb] ?? Infinity) > readRank);
}

// What a refusal should say. When the operation or the tool set raised the verb
// above what was declared, say so — an operator reading "approval-required" for
// a request they labelled "advise" needs to see why.
function actionLabel(binding) {
  if (!binding) return "advise";
  const verb = governingVerb(binding);
  const parts = [verb];
  if (binding.operation) parts.push(`operation ${binding.operation.name}${binding.operation.irreversible ? ", irreversible" : ""}`);
  if (binding.underDeclared) parts.push(`declared ${binding.declared}`);
  else if (binding.bound && binding.declared == null) parts.push("no verb declared");
  return parts.length > 1 ? `${parts[0]} (${parts.slice(1).join("; ")})` : verb;
}

// The compact form that goes on a decision and into the ledger.
function summarize(binding) {
  if (!binding) return null;
  return {
    declared: binding.declared, governing: governingVerb(binding), effective: binding.effective,
    bound: binding.bound, enforce: binding.enforce,
    operation: binding.operation ? binding.operation.name : null,
    irreversible: !!binding.operation?.irreversible,
    underDeclared: binding.underDeclared,
    notes: binding.notes.length ? binding.notes : undefined,
    tools: binding.narrowing?.available ? binding.narrowing.tools : null,
    wouldDrop: binding.narrowing?.available && binding.narrowing.dropped.length ? binding.narrowing.dropped : undefined,
  };
}

module.exports = {
  bindAction, governingVerb, requiresEnforcingVendor, secondVendorReadOnly, actionLabel, summarize,
  resolveOperation, narrowTools, toolClass,
  operationProblems, enforceableName, mcpServer, verbRanks, isKnownVerb, topVerb,
};
