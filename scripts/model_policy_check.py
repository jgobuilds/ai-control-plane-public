#!/usr/bin/env python3
"""Model-policy freshness check — the mechanism behind "update as models evolve".

A standard that says "review periodically" never gets reviewed. This turns the
cadence into a failing test:

  - the review date has passed                      -> stale, fix it
  - a routed model is deprecated or retired         -> migrate before it 404s
  - a retirement lands inside the warning horizon   -> migrate now, not later
  - intro pricing expires soon                      -> the cost model changes
  - router/policy.json and model-policy.json disagree about task types
  - a tier routes to a model that isn't in the catalog at all

Deterministic (Tier 0) — dates and JSON, no model call. Runs in CI.

    python scripts/model_policy_check.py
    python scripts/model_policy_check.py --json
    python scripts/model_policy_check.py --as-of 2026-12-01   # rehearse the future
"""
import os, sys, json, argparse, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RETIRE_WARN_DAYS = 90     # start shouting this far ahead of a retirement
PRICE_WARN_DAYS = 60      # ...and this far ahead of a pricing change

problems = []             # (severity, message)


def load(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return json.load(f)


def add(sev, msg):
    problems.append((sev, msg))


def days_until(date_str, today):
    return (datetime.date.fromisoformat(date_str) - today).days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD; defaults to today")
    a = ap.parse_args()
    today = (datetime.date.fromisoformat(a.as_of) if a.as_of
             else datetime.date.today())

    try:
        mp = load("context/model-policy.json")
        rp = load("router/policy.json")
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    catalog = {k: v for k, v in mp["catalog"].items() if not k.startswith("_")}
    work = {k: v for k, v in mp["workTypes"].items() if not k.startswith("_")}

    # --- 1. Is the policy itself stale? -----------------------------------
    overdue = -days_until(mp["reviewBy"], today)
    if overdue > 0:
        add("high", f"policy review overdue by {overdue} days "
                    f"(reviewBy {mp['reviewBy']}, last reviewed {mp['reviewedOn']}). "
                    f"Re-run the tier map against current models, then bump both dates.")
    else:
        add("info", f"review due in {-overdue} days ({mp['reviewBy']}).")

    # --- 2. Which models does the router actually route to? ---------------
    routed = {}
    for tier, spec in rp["tiers"].items():
        if spec.get("kind") == "model" and spec.get("model"):
            routed[spec["model"]] = tier

    for model, tier in routed.items():
        if model not in catalog:
            add("high", f"tier {tier} routes to '{model}', which is not in the "
                        f"model-policy catalog — its status and retirement date are unknown.")
            continue
        c = catalog[model]
        status = c.get("status", "unknown")
        if status == "retired":
            add("high", f"tier {tier} routes to RETIRED model '{model}' — requests will 404.")
        elif status == "deprecated":
            add("high", f"tier {tier} routes to deprecated model '{model}'"
                        + (f", retires {c['retiresOn']}" if c.get("retiresOn") else "")
                        + ". Migrate now.")

    # --- 3. Retirement horizon (catalog-wide, routed or not) -------------
    for model, c in catalog.items():
        if not c.get("retiresOn"):
            continue
        d = days_until(c["retiresOn"], today)
        where = f"routed at {routed[model]}" if model in routed else "not currently routed"
        if d < 0:
            add("high" if model in routed else "medium",
                f"'{model}' retired {-d} days ago ({c['retiresOn']}) — {where}. "
                f"Mark it status=retired.")
        elif d <= RETIRE_WARN_DAYS:
            add("high" if model in routed else "low",
                f"'{model}' retires in {d} days ({c['retiresOn']}) — {where}.")

    # --- 4. Pricing changes (intro pricing ending is a price rise) --------
    for model, c in catalog.items():
        if not c.get("introEndsOn"):
            continue
        d = days_until(c["introEndsOn"], today)
        if d < 0:
            add("medium", f"'{model}' intro pricing ended {-d} days ago "
                          f"({c['introEndsOn']}): now ${c.get('in')}/${c.get('out')} per 1M, "
                          f"up from ${c.get('introIn')}/${c.get('introOut')}. "
                          f"Update cost baselines and drop the intro fields.")
        elif d <= PRICE_WARN_DAYS:
            add("medium", f"'{model}' intro pricing ends in {d} days ({c['introEndsOn']}): "
                          f"${c.get('introIn')}/${c.get('introOut')} -> "
                          f"${c.get('in')}/${c.get('out')} per 1M. Re-baseline before it lands.")

    # --- 5. The two policy files must agree on task types ----------------
    task_types = {k for k in rp["taskTypes"] if not k.startswith("_")}
    for t in sorted(task_types - set(work)):
        add("medium", f"router taskType '{t}' has no entry in model-policy workTypes — "
                      f"it is routed with no recorded justification.")
    for t in sorted(set(work) - task_types):
        add("low", f"model-policy workType '{t}' is not a router taskType — dead entry.")
    for t in sorted(task_types & set(work)):
        rt, mt = rp["taskTypes"][t].get("tier"), work[t].get("tier")
        if rt != mt:
            add("high", f"task '{t}': router routes to {rt}, model-policy says {mt}. "
                        f"One of them is wrong.")

    # --- 6. Effort sanity -------------------------------------------------
    valid = set(mp["effort"]["guidance"])
    for t, spec in work.items():
        e = spec.get("effort")
        if e and e not in valid:
            add("medium", f"task '{t}' uses unknown effort '{e}' "
                          f"(valid: {', '.join(sorted(valid))}).")

    # --- report -----------------------------------------------------------
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    problems.sort(key=lambda p: order[p[0]])
    if a.json:
        print(json.dumps({"asOf": today.isoformat(),
                          "problems": [{"severity": s, "message": m} for s, m in problems]},
                         indent=2))
    else:
        print(f"Model policy check — as of {today.isoformat()}\n")
        for sev, msg in problems:
            print(f"  [{sev.upper():6}] {msg}")
        highs = sum(1 for s, _ in problems if s == "high")
        print(f"\n  {highs} blocking, {len(problems) - highs} advisory")

    return 1 if any(s == "high" for s, _ in problems) else 0


if __name__ == "__main__":
    sys.exit(main())
