#!/usr/bin/env python3
"""Daily digest — the human-awareness "digest" tier over the audit ledger.

The interrupt tier (CI failure alerts) tells a human to act NOW. The on-demand
tier (the ledger, eval_metrics.py) answers questions when someone investigates.
This fills the middle: a scheduled heartbeat that lets a person stay AWARE of
what the agents did, WITHOUT watching a dashboard — and that leads with anomalies
so it never becomes green-checkmark wallpaper.

It is a thin FORMATTER over scripts/eval_metrics.py — it does not re-read or
re-verify the ledger. `analyze()` already walks the hash chain, refuses a broken
one, reports coverage before every total, and computes drift. We turn its result
into a `{severity, title, message, source}` payload for the channel-abstracted
Notify sub-workflow (n8n), plus a `post` flag so the schedule can stay silent on
quiet days and only heartbeat once a week.

Design rules (see ai-standards/references/notification-taxonomy.md):
  - Anomaly-forward: drift / blocks / change-failures lead the body; counts follow.
  - Density-adaptive: post daily when there's activity; on a fully quiet day post
    ONLY on the heartbeat weekday (so a silent channel means "quiet", and a
    missing weekly heartbeat means the lane itself is down).
  - Coverage before any cost total; every cost is NOTIONAL, never "spend" (AIOPS.md).
  - Metadata only (the ledger is metadata-only by design — no prompts, no PII).
  - Never goes dark: any unexpected error is itself surfaced as an error post,
    never a crash and never silence (diagnosability.md — instrumentation must not
    break, and must not hide its own failure).

Pure stdlib. Windows-friendly. Run from the repo root:

    python scripts/daily_digest.py --days 1 --format text        # local preview
    python scripts/daily_digest.py --days 1 --format json /audit/decisions.jsonl
"""
import sys, os, json, argparse, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # so eval_metrics imports
import eval_metrics  # noqa: E402  (reused: analyze / default_ledger / verify+economics)

SOURCE = "digest"
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
# One honest line rather than a silent gap: CI failures already interrupt, so the
# digest does not re-summarize them in v1 (see the plan's out-of-scope note).
CI_NOTE = "CI health: covered by failure alerts (not summarized here)."


def _window_label(res):
    w = res.get("window") or {}
    since, until = (w.get("since") or "")[:10], (w.get("until") or "")[:10]
    return f"{since} → {until}" if since and until else "recent"


def _pct(v):
    """A percentage that reads 'n/a' rather than 'None%' when the metric is
    undefined (e.g. no verdicts to compute a verify-pass rate over)."""
    return f"{v}%" if v is not None else "n/a"


def summarize(res, run_date, heartbeat_weekday=0):
    """Pure: an eval_metrics.analyze() result + the run date -> a digest payload.

    Deterministic and side-effect-free so it is fully unit-testable without a
    clock or a ledger. `run_date` is a datetime.date; `heartbeat_weekday` is
    0=Mon..6=Sun. Returns {post, severity, title, message, source, meta}.
    """
    # 1. Broken hash chain — the loudest thing the digest can say. Surface it,
    #    do not try to compute over untrusted data (mirrors eval_metrics' refusal).
    if res.get("chain_ok") is False:
        return {
            "post": True, "severity": "error",
            "title": "Audit ledger REFUSED — hash chain broken",
            "message": (f"{res.get('error', 'chain broken')}\n"
                        "Metrics were NOT computed on untrusted data. "
                        "Run scripts/verify_audit.py to locate the tamper."),
            "source": SOURCE,
            "meta": {"chain_ok": False},
        }

    n = res.get("records_in_window", 0) or 0
    label = _window_label(res)

    # 2. No activity — density-adaptive. Silent on ordinary quiet days; a single
    #    weekly heartbeat proves the lane is alive (its ABSENCE is the signal).
    if n == 0 or "metrics" not in res:
        if run_date.weekday() == heartbeat_weekday:
            return {
                "post": True, "severity": "info",
                "title": f"Agent digest — quiet ({label})",
                "message": ("No agent decisions were recorded in the window. "
                            "This weekly heartbeat confirms the digest lane "
                            "itself is alive.\n" + CI_NOTE),
                "source": SOURCE,
                "meta": {"records_in_window": 0, "heartbeat": True},
            }
        return {
            "post": False, "severity": "info",
            "title": f"Agent digest — quiet, suppressed ({label})",
            "message": "No activity; not the heartbeat day; nothing posted.",
            "source": SOURCE,
            "meta": {"records_in_window": 0, "heartbeat": False},
        }

    # 3. Activity — anomaly-forward. What needs a look leads; the summary follows.
    m = res["metrics"]
    econ = m.get("economics") or {}
    drift = res.get("drift_flags") or []
    blocked = m.get("blocked", 0) or 0
    change_fail = ((m.get("verdicts") or {}).get("false", 0)) or 0

    lead = []
    for fl in drift:
        lead.append(f"⚠ drift [{fl.get('metric')}]: {fl.get('message')}")
    if blocked:
        reasons = ", ".join(f"{k}×{v['count']}"
                            for k, v in (m.get("block_reasons") or {}).items()
                            if v.get("count"))
        lead.append(f"⚠ blocked: {blocked}" + (f" ({reasons})" if reasons else ""))
    if change_fail:
        lead.append(f"⚠ change-failures (verdict=false): {change_fail}")

    # Coverage is stated BEFORE the cost total — a number from 3% of records is a
    # lie of omission (AIOPS.md). "notional", never "spend".
    cov = econ.get("usage_coverage_pct")
    per_completed = econ.get("notional_cost_per_completed_usd")
    econ_line = (f"notional $/completed: "
                 f"{per_completed if per_completed is not None else 'n/a'} "
                 f"(usage coverage {cov if cov is not None else 'n/a'}%)")

    summary = [
        f"activity: {m.get('attempted', 0)} attempted · "
        f"{m.get('completed', 0)} completed · {blocked} blocked",
        f"agentic leverage: {_pct(m.get('agentic_leverage_pct'))}   "
        f"verify-pass: {_pct(m.get('verify_pass_pct'))}",
        econ_line,
        CI_NOTE,
    ]

    severity = "warn" if (drift or blocked or change_fail) else "info"
    status = "needs a look" if severity == "warn" else "nominal"
    body = ("\n".join(lead) + "\n\n" if lead else "") + "\n".join(summary)
    return {
        "post": True, "severity": severity,
        "title": f"Agent digest — {status} ({label})",
        "message": body,
        "source": SOURCE,
        "meta": {"records_in_window": n, "drift": bool(drift),
                 "blocked": blocked, "change_failures": change_fail},
    }


def render_text(p):
    tag = "POST" if p.get("post") else "SKIP"
    return (f"[{tag}] ({p['severity']}) {p['title']}\n"
            f"{p['message']}\n--- source: {p['source']}")


def build(ledger, days, scope, heartbeat_weekday, now=None):
    """Wire analyze() -> summarize(). Never raises: an unexpected failure becomes
    an error payload so the digest surfaces its own breakage instead of vanishing."""
    try:
        end = now or datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(days=days)
        path = ledger or eval_metrics.default_ledger()
        if not os.path.exists(path):
            res = {"chain_ok": True, "records_in_window": 0,
                   "window": {"since": start.isoformat(), "until": end.isoformat(),
                              "scope": scope or "(all)"},
                   "note": f"no ledger at {path}"}
        else:
            res = eval_metrics.analyze(path, start, end, scope)
        return summarize(res, end.date(), heartbeat_weekday=heartbeat_weekday)
    except Exception as e:  # noqa: BLE001 — deliberately broad: never go dark
        return {
            "post": True, "severity": "error",
            "title": "Daily digest generator FAILED",
            "message": (f"{type(e).__name__}: {e}\n"
                        "The digest lane errored; this post is its own alarm."),
            "source": SOURCE,
            "meta": {"error": True},
        }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Daily digest over the audit ledger.")
    ap.add_argument("ledger", nargs="?", default=None, help="path to decisions.jsonl")
    ap.add_argument("--days", type=int, default=1, help="window length in days (default 1)")
    ap.add_argument("--scope", default=None, help="restrict to one scope")
    ap.add_argument("--heartbeat-weekday", default="mon",
                    help="weekday to post a heartbeat on a quiet day (default mon)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    hb = WEEKDAYS.get(str(args.heartbeat_weekday).lower(), 0)
    payload = build(args.ledger, args.days, args.scope, hb)
    print(json.dumps(payload) if args.format == "json" else render_text(payload))
    return 0  # always 0: n8n treats stdout as data; the payload carries severity


if __name__ == "__main__":
    sys.exit(main())
