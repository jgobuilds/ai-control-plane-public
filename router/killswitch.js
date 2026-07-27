// Kill switch + budget/rate circuit breaker (governance control — the ability to
// halt agent activity instantly is table-stakes governance).
//
// Two independent halts, both evaluated at the very TOP of the router pipeline
// (before any scope work or dispatch), so an operator can stop agent activity
// instantly and a runaway loop trips itself:
//
//   1. Kill switch  — control/halt.json (RUNTIME STATE, gitignored). An operator
//      writes it via scripts/halt.py. A global halt stops everything; a scope
//      halt stops that scope AND every descendant — we match halt.json against
//      the request's full scope CHAIN, so an ancestor being halted halts the
//      child (halting "consulting" halts "client-a").
//   2. Circuit breaker — a rate/budget ceiling. Counts recent decisions in the
//      audit ledger within a sliding window and trips if a per-scope or global
//      threshold is met. Config lives in policy.json `limits`.
//
// Pure/fs functions, no dependencies (core only), and FAIL SAFE for AVAILABILITY:
// a missing/corrupt halt file or an unreadable ledger must NOT wedge the router —
// we log and treat as "not halted / not tripped". The kill switch is an operator
// escalation, not a security boundary; on ambiguity, availability wins. (Contrast
// with the auth/approval controls, which fail CLOSED.)
"use strict";

const fs = require("fs");
const path = require("path");

const CONTROL_DIR = process.env.CONTROL_DIR || "/control";

// haltState(dir) — read control/halt.json. Missing or unparseable => not halted
// (fail SAFE), but a real read/parse error (not ENOENT) is logged so the
// condition is visible. Returns a normalized state object.
function haltState(dir) {
  const file = path.join(dir || CONTROL_DIR, "halt.json");
  try {
    const s = JSON.parse(fs.readFileSync(file, "utf8"));
    return {
      global: !!s.global,
      scopes: Array.isArray(s.scopes) ? s.scopes.filter((x) => typeof x === "string" && x) : [],
      reason: typeof s.reason === "string" ? s.reason : "",
      ts: typeof s.ts === "string" ? s.ts : "",
    };
  } catch (e) {
    if (e && e.code !== "ENOENT")
      console.error("killswitch: halt.json unreadable, treating as NOT halted:", e.message);
    return { global: false, scopes: [], reason: "", ts: "" };
  }
}

// isHalted(state, scopeChain) -> { halted, reason }
// Halted if the global switch is on OR any scope in the request's chain (the
// scope itself or any ancestor) is listed. Matching an ancestor halts every
// descendant — what an operator halting a parent scope expects.
function isHalted(state, scopeChain) {
  const s = state || {};
  if (s.global)
    return { halted: true, reason: s.reason ? `global halt: ${s.reason}` : "global halt" };
  const listed = Array.isArray(s.scopes) ? s.scopes : [];
  const chain = Array.isArray(scopeChain) ? scopeChain : scopeChain ? [scopeChain] : [];
  const hit = chain.find((sc) => listed.includes(sc));
  if (hit)
    return { halted: true, reason: s.reason ? `scope "${hit}" halted: ${s.reason}` : `scope "${hit}" halted` };
  return { halted: false, reason: "" };
}

// breaker(policy, auditPath, scopeName) -> { tripped, reason }
// Budget/rate circuit breaker. Reads policy.limits { windowMinutes,
// maxRequestsPerScope, maxRequestsGlobal }, counts decisions in the audit ledger
// whose ts falls inside the sliding window, and trips if the global count or the
// per-scope count meets/exceeds its ceiling. Deterministic + cheap: one line
// scan of the ledger, comparing each record's ts against the window cutoff.
// A ceiling that is null/absent/non-finite disables that dimension.
function breaker(policy, auditPath, scopeName) {
  const limits = policy && policy.limits;
  if (!limits || typeof limits !== "object") return { tripped: false, reason: "" };
  const windowMin = Number(limits.windowMinutes);
  if (!(windowMin > 0)) return { tripped: false, reason: "" };
  const maxGlobal = Number.isFinite(Number(limits.maxRequestsGlobal)) ? Number(limits.maxRequestsGlobal) : Infinity;
  const maxScope = Number.isFinite(Number(limits.maxRequestsPerScope)) ? Number(limits.maxRequestsPerScope) : Infinity;
  // Spend ceilings. A request count treats a t1 classify and a t3 xhigh
  // architecture run as equal when they differ by orders of magnitude — so the
  // rate ceiling alone is a poor proxy for a runaway. These bound NOTIONAL cost
  // (see AIOPS.md): not money spent under subscription auth, but the only
  // comparable yardstick across tiers, which is exactly what a ceiling needs.
  const maxSpendGlobal = Number.isFinite(Number(limits.maxNotionalSpendGlobal)) ? Number(limits.maxNotionalSpendGlobal) : Infinity;
  const maxSpendScope = Number.isFinite(Number(limits.maxNotionalSpendPerScope)) ? Number(limits.maxNotionalSpendPerScope) : Infinity;
  if (!(maxGlobal < Infinity) && !(maxScope < Infinity)
      && !(maxSpendGlobal < Infinity) && !(maxSpendScope < Infinity))
    return { tripped: false, reason: "" };

  let raw;
  try {
    raw = fs.readFileSync(auditPath, "utf8");
  } catch (e) {
    if (e && e.code !== "ENOENT")
      console.error("killswitch: ledger unreadable, breaker treated as CLOSED:", e.message);
    return { tripped: false, reason: "" }; // no ledger => nothing to count
  }

  const cutoff = Date.now() - windowMin * 60 * 1000;
  let globalN = 0, scopeN = 0, globalSpend = 0, scopeSpend = 0;
  for (const line of raw.split("\n")) {
    if (!line) continue;
    const sp = line.indexOf(" ");
    if (sp < 0) continue;
    let rec;
    try { rec = JSON.parse(line.slice(sp + 1)); } catch { continue; }
    const t = Date.parse(rec.ts);
    if (!(t >= cutoff)) continue; // outside the window (also skips unparseable ts)
    globalN++;
    // Records written before economics existed have no usage block; treating a
    // missing cost as 0 keeps the breaker permissive on old data rather than
    // tripping on absence. Availability wins on ambiguity, as elsewhere here.
    const c = Number(rec.usage && rec.usage.notionalCostUsd);
    const cost = Number.isFinite(c) ? c : 0;
    globalSpend += cost;
    if (scopeName && rec.scope === scopeName) { scopeN++; scopeSpend += cost; }
  }

  const usd = (n) => "$" + n.toFixed(4);

  if (globalN >= maxGlobal)
    return { tripped: true, reason: `circuit breaker: ${globalN} requests in ${windowMin}m meets/exceeds global ceiling ${maxGlobal}` };
  if (scopeName && scopeN >= maxScope)
    return { tripped: true, reason: `circuit breaker: scope "${scopeName}" made ${scopeN} requests in ${windowMin}m, meets/exceeds per-scope ceiling ${maxScope}` };
  if (globalSpend >= maxSpendGlobal)
    return { tripped: true, reason: `circuit breaker: ${usd(globalSpend)} notional in ${windowMin}m meets/exceeds global spend ceiling ${usd(maxSpendGlobal)}` };
  if (scopeName && scopeSpend >= maxSpendScope)
    return { tripped: true, reason: `circuit breaker: scope "${scopeName}" spent ${usd(scopeSpend)} notional in ${windowMin}m, meets/exceeds per-scope ceiling ${usd(maxSpendScope)}` };
  return { tripped: false, reason: "" };
}

module.exports = { haltState, isHalted, breaker, CONTROL_DIR };
