#!/usr/bin/env python3
"""Eval / drift lane — agentic-leverage + quality metrics from the audit ledger.

Reads the router's tamper-evident audit ledger (audit/decisions.jsonl) and derives
the market's "continuous eval" (Arthur/Fiddler/Galileo) from data we ALREADY log:
agentic leverage, verify-pass / change-failure, governance-friction (block) rates,
guardrail / PII / approval rates, and the risk / tier / model / provider mix. It
trends those across two sub-windows and raises drift WARNs when a key metric moves
beyond a threshold.

HONEST SCOPE. This is a metadata-derived proxy over what the harness recorded — the
fresh-context verifier's pass/fail is the quality signal — NOT a semantic or
ground-truth eval. It measures what the ledger captured, not correctness. See EVAL.md.

The ledger is only trusted if its hash chain verifies. We re-walk the chain with the
SAME logic as scripts/verify_audit.py FIRST; a broken chain is REFUSED (we do not
compute metrics on data we can't trust) and reported as chain_ok:false / exit 2.

Pure stdlib. Windows-friendly.

    python scripts/eval_metrics.py                       # last 7 days, text
    python scripts/eval_metrics.py --days 30 --scope jane
    python scripts/eval_metrics.py --since 2026-07-01 --format json
"""
import sys, os, json, hashlib, argparse, datetime

# --------------------------------------------------------------------------- #
# Drift thresholds. A metric that moves past one of these across the window's
# first vs second half raises a WARN. Tune here; documented in EVAL.md.
DRIFT = {
    # verify-pass rate (quality) DROPS by more than this many percentage points.
    "verify_pass_drop_pp": 10.0,
    # change-failure proxy (verdict-false rate) RISES by more than this many pp.
    "change_failure_rise_pp": 10.0,
    # overall block rate (governance friction) more than MULTIPLIES by this factor
    # (or crosses the floor below when it was ~zero in the first half).
    "block_rate_rise_factor": 2.0,
    "block_rate_floor": 0.05,
    # critical-risk share RISES by more than this many percentage points.
    "critical_share_rise_pp": 10.0,
}

# Canonical block reasons (router `blocked` strings). PII blocks surface as an
# "error:PII detected…" string (the router throws), so we fold those into "pii".
BLOCK_REASONS = ["halted", "circuit-open", "approval-required", "approval-unauthorized",
                 "guardrail-input", "mode-forbids-action", "pii", "error"]
RISK_LEVELS = ["low", "medium", "high", "critical"]

# --------------------------------------------------------------------------- #
# HARNESS EVAL — score the harness, config and model TOGETHER, not the model
# alone. Borrowed framing (clawbench, 2026-07-26 review): a run's outcome is
# produced by a configuration, not by an LLM in isolation, so "which model is
# better" is the wrong question when tier, operating mode and whether a verifier
# ran are all varying underneath it. The metrics above aggregate ACROSS every
# configuration and therefore average away exactly the thing you would change.
#
# The floor below is the honest part. A per-config difference computed from two
# runs is noise wearing a decimal point, and a table that ranks them anyway will
# be believed. Below this n a config is REPORTED but never RANKED.
HARNESS_MIN_N = 5

# The leverage proxy definition, stated in every output so it's never implicit.
LEVERAGE_PROXY = (
    "share of attempted decisions produced with the quality bar held = "
    "(verdict==true) OR (no verify required AND not blocked), over all attempted. "
    "Proxy over logged metadata (verifier pass), NOT a semantic/ground-truth eval."
)


def sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_ledger():
    return os.path.join(repo_root(), "audit", "decisions.jsonl")


def verify_chain(path):
    """Re-walk the hash chain exactly like scripts/verify_audit.py.

    Returns (ok, error_or_None, records). We refuse to trust — and refuse to
    measure — a ledger whose chain is broken."""
    prev, records = "GENESIS", []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line:
                continue
            h, _, j = line.partition(" ")
            if sha256(prev + j) != h:
                return False, f"hash mismatch at line {i} (chain broken)", records
            try:
                rec = json.loads(j)
            except json.JSONDecodeError:
                return False, f"unparseable json at line {i}", records
            if rec.get("prevHash") != prev:
                return False, f"prevHash mismatch at line {i}", records
            records.append(rec)
            prev = h
    return True, None, records


def parse_ts(s):
    if not s:
        return None
    try:
        s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


def block_bucket(reason):
    """Normalize a router `blocked` string into a canonical reason bucket."""
    if not reason:
        return None
    if reason in BLOCK_REASONS:
        return reason
    if reason.startswith("error:"):
        return "pii" if "pii" in reason.lower() else "error"
    return reason  # unknown reason — keep verbatim so it's visible, not swallowed


def is_blocked(r):
    return r.get("blocked") is not None


def quality_held(r):
    """Agentic-leverage numerator condition (see LEVERAGE_PROXY)."""
    v = r.get("verdict")
    if v is True:
        return True
    if v is None and not is_blocked(r):
        return True
    return False


def pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def harness_signature(r):
    """The CONFIGURATION that produced a decision — everything we choose, not
    just the model.

    Deliberately includes `verify` and `mode`: a t2 run with a verifier and a t2
    run without are different harnesses that happen to share a model, and the
    operating mode caps what the agent may do at all. Excluding them is how a
    model gets blamed for a configuration's result.
    """
    tier = r.get("tier") or "?"
    provider = r.get("provider") or "-"
    model = r.get("model") or "-"
    mode = r.get("mode") or "-"
    verify = "verify" if r.get("verdict") is not None else "no-verify"
    return f"{tier} {provider}:{model} [{mode}, {verify}]"


def harness_breakdown(records, min_n=HARNESS_MIN_N):
    """Per-configuration outcomes, and a ranking ONLY where n justifies one.

    Returns configs sorted by sample size. `comparable` holds the subset with
    enough runs to be worth reading against each other; when fewer than two
    configs clear the floor there is nothing to compare and the note says so
    rather than presenting a one-row 'ranking'.
    """
    groups = {}
    for r in records:
        groups.setdefault(harness_signature(r), []).append(r)

    configs = []
    for sig, rs in groups.items():
        n = len(rs)
        verdicts = [r for r in rs if r.get("verdict") is not None]
        passed = [r for r in verdicts if r.get("verdict") is True]
        blocked = [r for r in rs if is_blocked(r)]
        held = [r for r in rs if quality_held(r)]
        costs = [(r.get("usage") or {}).get("notionalCostUsd") for r in rs]
        costs = [c for c in costs if isinstance(c, (int, float))]
        durs = [(r.get("usage") or {}).get("durationMs") for r in rs]
        durs = [d for d in durs if isinstance(d, (int, float))]
        configs.append({
            "config": sig,
            "n": n,
            # Stated as counts as well as percentages: "60%" from 5 runs and from
            # 500 read identically, and only one of them means anything.
            "verified": len(verdicts),
            "verify_pass_pct": pct(len(passed), len(verdicts)),
            "blocked": len(blocked),
            "quality_held_pct": pct(len(held), n),
            "notional_cost_usd": round(sum(costs), 4) if costs else None,
            "cost_per_held_usd": (round(sum(costs) / len(held), 4)
                                  if costs and held else None),
            "median_duration_ms": (sorted(durs)[len(durs) // 2] if durs else None),
            "sufficient": n >= min_n,
        })
    configs.sort(key=lambda c: (-c["n"], c["config"]))

    comparable = [c for c in configs if c["sufficient"]]
    if len(comparable) < 2:
        note = (f"NOT ENOUGH DATA TO COMPARE. {len(comparable)} of "
                f"{len(configs)} configuration(s) reached n>={min_n}. Per-config "
                f"numbers below are descriptive only — do not read them as a "
                f"ranking, and do not change the harness on this basis.")
    else:
        note = (f"{len(comparable)} configuration(s) reached n>={min_n} and are "
                f"comparable. This is a directional read over logged metadata, "
                f"not a significance test.")
    return {"min_n": min_n, "configs": configs,
            "comparable": [c["config"] for c in comparable], "note": note}


def compute_metrics(records):
    """Compute the metric blob for a set of records. Shares are over ALL attempted
    decisions (a single, consistent denominator) unless noted."""
    total = len(records)
    if total == 0:
        return {"attempted": 0}

    blocked = [r for r in records if is_blocked(r)]
    completed = total - len(blocked)

    # Quality signal from the fresh-context verifier.
    v_true = sum(1 for r in records if r.get("verdict") is True)
    v_false = sum(1 for r in records if r.get("verdict") is False)
    v_judged = v_true + v_false

    held = sum(1 for r in records if quality_held(r))

    # Block reasons.
    reason_counts = {k: 0 for k in BLOCK_REASONS}
    for r in blocked:
        b = block_bucket(r.get("blocked"))
        reason_counts[b] = reason_counts.get(b, 0) + 1

    # Risk-level mix (share of all attempted).
    risk_counts = {k: 0 for k in RISK_LEVELS}
    risk_bearing = 0
    for r in records:
        lvl = (r.get("risk") or {}).get("level")
        if lvl is not None:
            risk_bearing += 1
            risk_counts[lvl] = risk_counts.get(lvl, 0) + 1

    def mix(field):
        out = {}
        for r in records:
            val = r.get(field)
            if val is not None:
                out[val] = out.get(val, 0) + 1
        return {k: {"count": v, "pct": pct(v, total)} for k, v in sorted(out.items())}

    guardrail_findings = sum(1 for r in records if r.get("guardrails"))
    approvals = sum(1 for r in records if r.get("approved") is True)

    return {
        "attempted": total,
        "completed": completed,
        "blocked": len(blocked),
        # Headline.
        "agentic_leverage_pct": pct(held, total),
        "leverage_numerator": held,
        # Quality.
        "verify_pass_pct": pct(v_true, v_judged),
        "change_failure_pct": pct(v_false, v_judged),
        "verdicts": {"true": v_true, "false": v_false, "judged": v_judged,
                     "none_required": total - v_judged},
        # Governance friction / incidents.
        "block_rate_pct": pct(len(blocked), total),
        "block_reasons": {k: {"count": reason_counts[k], "pct": pct(reason_counts[k], total)}
                          for k in reason_counts},
        "guardrail_finding_pct": pct(guardrail_findings, total),
        "pii_block_pct": pct(reason_counts.get("pii", 0), total),
        "approval_pct": pct(approvals, total),
        # Mixes (cost / risk proxies).
        "risk_level_mix": {k: {"count": risk_counts[k], "pct": pct(risk_counts[k], total)}
                           for k in RISK_LEVELS},
        "risk_bearing": risk_bearing,
        "tier_mix": mix("tier"),
        "provider_mix": mix("provider"),
        "model_mix": mix("model"),
        # Unit economics (notional — see AIOPS.md).
        "economics": economics(records, held),
    }


def economics(records, held):
    """Unit economics from the ledger's `usage` block.

    The headline is cost per COMPLETED task, not per call. Per-call cost makes
    the wrong choice look right: a higher effort setting costs more per call and
    frequently less per finished task, because it cuts the number of turns. Only
    the per-completed-task number can see that.

    Every value is NOTIONAL — what the work would cost at metered API rates.
    Runners authenticate on a subscription, so this is a comparable yardstick
    across tiers, not money spent. See AIOPS.md.
    """
    used = [r for r in records if isinstance(r.get("usage"), dict)]
    coverage = pct(len(used), len(records))

    def total(field):
        vals = [r["usage"].get(field) for r in used]
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(sum(vals), 6) if vals else None

    cost = total("notionalCostUsd")
    turns = total("turns")
    dur = total("durationMs")
    per_tier = {}
    for r in used:
        t = r.get("tier") or "unknown"
        c = r["usage"].get("notionalCostUsd")
        if isinstance(c, (int, float)):
            e = per_tier.setdefault(t, {"n": 0, "usd": 0.0})
            e["n"] += 1
            e["usd"] = round(e["usd"] + c, 6)

    return {
        # Coverage first: a cost number computed from 3% of records is a lie of
        # omission, so it is stated before any total.
        "usage_coverage_pct": coverage,
        "records_with_usage": len(used),
        "notional_cost_usd_total": cost,
        # THE headline unit. Denominator is quality-held completions, so work
        # that failed the bar is counted as cost without benefit -- which is
        # exactly what it was.
        "notional_cost_per_completed_usd": (round(cost / held, 6)
                                            if cost is not None and held else None),
        "notional_cost_per_attempt_usd": (round(cost / len(used), 6)
                                          if cost is not None and used else None),
        "avg_turns_per_attempt": (round(turns / len(used), 2)
                                  if turns is not None and used else None),
        "avg_duration_ms": (round(dur / len(used)) if dur is not None and used else None),
        "notional_cost_by_tier": {k: {"count": v["n"], "usd": v["usd"],
                                      "usd_per_call": round(v["usd"] / v["n"], 6)}
                                  for k, v in sorted(per_tier.items())},
    }


def drift_flags(first, second):
    """Compare two sub-window metric blobs; emit WARN dicts past a threshold."""
    flags = []
    if first.get("attempted", 0) == 0 or second.get("attempted", 0) == 0:
        return flags  # can't trend a half with no data

    # 1. verify-pass rate drop (quality regression).
    a, b = first.get("verify_pass_pct"), second.get("verify_pass_pct")
    if a is not None and b is not None and (a - b) > DRIFT["verify_pass_drop_pp"]:
        flags.append({"metric": "verify_pass_pct", "first": a, "second": b,
                      "message": f"verify-pass rate fell {round(a - b, 1)}pp "
                                 f"({a}% -> {b}%), threshold {DRIFT['verify_pass_drop_pp']}pp"})

    # 2. change-failure (verdict-false) rise.
    a, b = first.get("change_failure_pct"), second.get("change_failure_pct")
    if a is not None and b is not None and (b - a) > DRIFT["change_failure_rise_pp"]:
        flags.append({"metric": "change_failure_pct", "first": a, "second": b,
                      "message": f"change-failure proxy rose {round(b - a, 1)}pp "
                                 f"({a}% -> {b}%), threshold {DRIFT['change_failure_rise_pp']}pp"})

    # 3. block rate spike (governance friction).
    a = first.get("block_rate_pct") or 0.0
    b = second.get("block_rate_pct") or 0.0
    factor = DRIFT["block_rate_rise_factor"]
    floor = DRIFT["block_rate_floor"] * 100.0
    spike = (b > a * factor) if a > 0 else (b > floor)
    if spike and b > a:
        flags.append({"metric": "block_rate_pct", "first": a, "second": b,
                      "message": f"block rate rose {a}% -> {b}% "
                                 f"(>{factor}x, or crossed {floor}% floor from ~0)"})

    # 4. critical-risk share rise.
    a = (first.get("risk_level_mix", {}).get("critical", {}) or {}).get("pct") or 0.0
    b = (second.get("risk_level_mix", {}).get("critical", {}) or {}).get("pct") or 0.0
    if (b - a) > DRIFT["critical_share_rise_pp"]:
        flags.append({"metric": "critical_risk_share_pct", "first": a, "second": b,
                      "message": f"critical-risk share rose {round(b - a, 1)}pp "
                                 f"({a}% -> {b}%), threshold {DRIFT['critical_share_rise_pp']}pp"})

    return flags


def analyze(path, start, end, scope):
    ok, err, all_records = verify_chain(path)
    if not ok:
        return {"chain_ok": False, "error": err,
                "note": "REFUSED — ledger hash chain broken; metrics not computed."}

    # Window + scope filter. Records without a parseable ts are dropped from the
    # windowed analysis (they can't be placed in time).
    windowed = []
    for r in all_records:
        ts = parse_ts(r.get("ts"))
        if ts is None:
            continue
        if ts < start or ts > end:
            continue
        if scope and r.get("scope") != scope:
            continue
        r["_ts"] = ts
        windowed.append(r)

    result = {
        "chain_ok": True,
        "ledger_records_total": len(all_records),
        "window": {"since": start.isoformat(), "until": end.isoformat(),
                   "scope": scope or "(all)"},
        "leverage_proxy": LEVERAGE_PROXY,
        "records_in_window": len(windowed),
    }
    if not windowed:
        result["note"] = "no data yet (empty window)"
        return result

    result["metrics"] = compute_metrics(windowed)

    # Drift: split the window in half by TIME (first half vs second half).
    mid = start + (end - start) / 2
    first = [r for r in windowed if r["_ts"] < mid]
    second = [r for r in windowed if r["_ts"] >= mid]
    fm, sm = compute_metrics(first), compute_metrics(second)
    flags = drift_flags(fm, sm)
    result["subwindows"] = {
        "split_at": mid.isoformat(),
        "first_half": {"attempted": fm.get("attempted", 0),
                       "verify_pass_pct": fm.get("verify_pass_pct"),
                       "change_failure_pct": fm.get("change_failure_pct"),
                       "block_rate_pct": fm.get("block_rate_pct"),
                       "agentic_leverage_pct": fm.get("agentic_leverage_pct")},
        "second_half": {"attempted": sm.get("attempted", 0),
                        "verify_pass_pct": sm.get("verify_pass_pct"),
                        "change_failure_pct": sm.get("change_failure_pct"),
                        "block_rate_pct": sm.get("block_rate_pct"),
                        "agentic_leverage_pct": sm.get("agentic_leverage_pct")},
    }
    result["drift_flags"] = flags
    result["drift"] = bool(flags)

    # Harness eval: the same window, split by CONFIGURATION rather than by time.
    # Drift tells you something changed; this tells you which setup produced it.
    result["harness"] = harness_breakdown(windowed)
    return result


def render_text(res):
    L = []
    if not res.get("chain_ok", True):
        L.append("EVAL — REFUSED")
        L.append(f"  ledger hash chain BROKEN: {res.get('error')}")
        L.append("  Metrics NOT computed on untrusted data. Run scripts/verify_audit.py.")
        return "\n".join(L)

    w = res["window"]
    L.append("Eval / drift — agentic leverage + quality from the audit ledger")
    L.append(f"  window: {w['since']}  ->  {w['until']}   scope: {w['scope']}")
    L.append(f"  ledger records (total): {res['ledger_records_total']}   in window: {res['records_in_window']}")
    L.append(f"  proxy: {res['leverage_proxy']}")

    if res.get("note") and "metrics" not in res:
        L.append(f"  {res['note']}")
        return "\n".join(L)

    m = res["metrics"]
    L.append("")
    L.append(f"  AGENTIC LEVERAGE (headline): {m['agentic_leverage_pct']}%  "
             f"({m['leverage_numerator']}/{m['attempted']} attempted held the bar)")
    L.append(f"  completed: {m['completed']}   blocked: {m['blocked']}")
    L.append("")
    L.append("  Quality (fresh-context verifier):")
    v = m["verdicts"]
    L.append(f"    verify-pass: {m['verify_pass_pct']}%   change-failure: {m['change_failure_pct']}%   "
             f"(true={v['true']} false={v['false']} judged={v['judged']} no-verify={v['none_required']})")
    L.append("")
    L.append(f"  Governance friction — block rate: {m['block_rate_pct']}%")
    for k in BLOCK_REASONS:
        c = m["block_reasons"][k]
        if c["count"]:
            L.append(f"    {k:<22} {c['count']:>4}  ({c['pct']}%)")
    L.append(f"    guardrail-finding rate: {m['guardrail_finding_pct']}%   "
             f"PII-block rate: {m['pii_block_pct']}%   approval rate: {m['approval_pct']}%")
    L.append("")
    L.append(f"  Risk-level mix (of {m['risk_bearing']} risk-scored):")
    for k in RISK_LEVELS:
        c = m["risk_level_mix"][k]
        L.append(f"    {k:<10} {c['count']:>4}  ({c['pct']}%)")
    for label, field in (("Tier", "tier_mix"), ("Provider", "provider_mix"), ("Model", "model_mix")):
        mix = m[field]
        if mix:
            parts = ", ".join(f"{k}={vv['count']} ({vv['pct']}%)" for k, vv in mix.items())
            L.append(f"  {label} mix: {parts}")

    sw = res["subwindows"]
    L.append("")
    L.append(f"  Drift (split {sw['split_at']}):")
    f, s = sw["first_half"], sw["second_half"]
    L.append(f"    first  half (n={f['attempted']}): leverage {f['agentic_leverage_pct']}%  "
             f"verify-pass {f['verify_pass_pct']}%  change-fail {f['change_failure_pct']}%  block {f['block_rate_pct']}%")
    L.append(f"    second half (n={s['attempted']}): leverage {s['agentic_leverage_pct']}%  "
             f"verify-pass {s['verify_pass_pct']}%  change-fail {s['change_failure_pct']}%  block {s['block_rate_pct']}%")
    if res["drift_flags"]:
        for fl in res["drift_flags"]:
            L.append(f"    WARN  [{fl['metric']}] {fl['message']}")
    else:
        L.append("    no drift flags (all key metrics within threshold)")

    h = res.get("harness")
    if h:
        L.append("")
        L.append("  HARNESS (the configuration produced the result, not the model alone):")
        for c in h["configs"]:
            mark = " " if c["sufficient"] else "*"
            L.append(f"   {mark} n={c['n']:<3} verify-pass {str(c['verify_pass_pct']):>5}%  "
                     f"held {str(c['quality_held_pct']):>5}%  "
                     f"$/held {c['cost_per_held_usd']}  {c['config']}")
        if any(not c["sufficient"] for c in h["configs"]):
            L.append(f"     * fewer than n={h['min_n']} — shown, NOT ranked.")
        L.append(f"     {h['note']}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Eval/drift metrics from the audit ledger.")
    ap.add_argument("ledger", nargs="?", default=None, help="path to decisions.jsonl")
    ap.add_argument("--since", default=None, help="ISO date/datetime window start (overrides --days)")
    ap.add_argument("--days", type=int, default=7, help="window length in days (default 7)")
    ap.add_argument("--scope", default=None, help="restrict to one scope")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    path = args.ledger or default_ledger()
    end = datetime.datetime.now(datetime.timezone.utc)
    if args.since:
        start = parse_ts(args.since)
        if start is None:
            print(f"bad --since value: {args.since!r}", file=sys.stderr)
            return 2
    else:
        start = end - datetime.timedelta(days=args.days)

    if not os.path.exists(path):
        res = {"chain_ok": True, "ledger_records_total": 0, "records_in_window": 0,
               "window": {"since": start.isoformat(), "until": end.isoformat(),
                          "scope": args.scope or "(all)"},
               "leverage_proxy": LEVERAGE_PROXY,
               "note": f"no ledger at {path} (no data yet)"}
        print(json.dumps(res, indent=2) if args.format == "json" else render_text(res))
        return 0

    res = analyze(path, start, end, args.scope)

    # Strip internal fields before emitting.
    print(json.dumps(res, indent=2, default=str) if args.format == "json" else render_text(res))

    if not res.get("chain_ok", True):
        return 2  # tamper: refuse
    return 0


if __name__ == "__main__":
    sys.exit(main())
