#!/usr/bin/env python3
"""Historical accuracy + SEMANTIC drift over the eval results ledger.

WHAT THIS ADDS. `eval_metrics.py` trends metadata the router logged — a proxy,
by its own admission. `eval_run.py` grades real output but used to print a
verdict and vanish, so the semantic half had no history at all: "is quality
drifting?" could only be answered about metadata. This reads the ledger
`eval_run --record` appends to and answers it about graded output.

THREE THINGS, and the third is the one worth having:

  TREND      pass rate and mean confidence per run, newest last.
  DRIFT      the latest run against a baseline of the runs before it. Thresholds
             mirror eval_metrics.py's DRIFT dict so both halves of eval speak the
             same language.
  FLIPS      cases that changed verdict between the last two runs of a label.
             A pass rate that holds while two cases swap pass and fail is not a
             stable suite, and the aggregate cannot show you that.

Runs are compared WITHIN a (dataset, label) pair. Comparing a t3 variant against
a t1 baseline and calling the difference drift would be a category error — that
is an A/B, which is eval_ab.py.

    python scripts/eval_history.py                    # trend every label
    python scripts/eval_history.py --label baseline   # one label
    python scripts/eval_history.py --fail-on-drift    # CI gate

Pure stdlib.
"""
import argparse, io, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.environ.get("EVAL_RESULTS", os.path.join(ROOT, "eval", "results.jsonl"))

# Same shape and spirit as eval_metrics.DRIFT: a documented dict at the top, not
# a magic number buried in a comparison.
DRIFT = {
    "pass_rate_drop_pp": 10.0,      # pass rate falling more than N points
    "confidence_drop_pp": 15.0,     # judges agreeing less than they did
    "min_baseline_runs": 2,         # below this there is no baseline worth the name
}


def load(path):
    if not os.path.isfile(path):
        return []
    out = []
    with io.open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"  warn  {path}:{i} is not valid JSON — skipped", file=sys.stderr)
    return out


def pct(x):
    return "  n/a" if x is None else f"{x * 100:5.1f}%"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Trend eval accuracy and detect semantic drift.")
    ap.add_argument("--results", default=RESULTS)
    ap.add_argument("--label", default=None, help="only this run label")
    ap.add_argument("--dataset", default=None, help="only this dataset")
    ap.add_argument("--last", type=int, default=10, help="rows to show per group")
    ap.add_argument("--fail-on-drift", action="store_true", help="exit 1 if drift is flagged")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    rows = load(args.results)
    if args.label:
        rows = [r for r in rows if r.get("label") == args.label]
    if args.dataset:
        rows = [r for r in rows if r.get("dataset") == args.dataset]
    if not rows:
        # Not a failure. An empty ledger means nobody has run `--record` yet, and
        # exiting non-zero would make a fresh checkout look broken.
        print(f"No eval results in {args.results}. Run:\n"
              "  python scripts/eval_run.py --dataset eval/datasets/starter "
              "--offline outputs.jsonl --record")
        return 0

    groups = {}
    for r in rows:
        groups.setdefault((r.get("dataset"), r.get("label")), []).append(r)

    report, drifted = [], []
    for (dataset, label), runs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        runs.sort(key=lambda r: r.get("ts") or "")
        entry = {"dataset": dataset, "label": label, "runs": len(runs), "drift": []}
        print(f"\n{dataset}  ·  label={label}  ({len(runs)} run(s))")
        print("  ts                        sha       pass    conf    cases")
        for r in runs[-args.last:]:
            print(f"  {r.get('ts','?'):<25} {str(r.get('sha') or '-'):<9} "
                  f"{pct(r.get('passRate'))}  {pct(r.get('meanConfidence'))}  "
                  f"{r.get('passed')}/{r.get('total')}")

        latest = runs[-1]
        baseline = runs[:-1]
        if len(baseline) < DRIFT["min_baseline_runs"]:
            print(f"  (no drift check — needs {DRIFT['min_baseline_runs']} prior run(s), "
                  f"has {len(baseline)})")
        else:
            def mean(key):
                vals = [r[key] for r in baseline if r.get(key) is not None]
                return sum(vals) / len(vals) if vals else None
            for key, flag, thresh in (("passRate", "pass_rate", DRIFT["pass_rate_drop_pp"]),
                                      ("meanConfidence", "confidence", DRIFT["confidence_drop_pp"])):
                base, now = mean(key), latest.get(key)
                if base is None or now is None:
                    continue
                drop = (base - now) * 100
                if drop > thresh:
                    msg = (f"{flag} fell {drop:.1f}pp vs a {len(baseline)}-run baseline "
                           f"({base * 100:.1f}% -> {now * 100:.1f}%)")
                    print(f"  DRIFT  {msg}")
                    entry["drift"].append(msg)
                    drifted.append(f"{dataset}/{label}: {msg}")

        # Per-case flips. The aggregate can hold steady while the suite churns
        # underneath it, and that is a different problem with a different cause.
        if len(runs) >= 2:
            prev, cur = runs[-2].get("byCase") or {}, latest.get("byCase") or {}
            broke = sorted(c for c in cur if prev.get(c) is True and cur[c] is False)
            fixed = sorted(c for c in cur if prev.get(c) is False and cur[c] is True)
            if broke:
                print("  BROKE  " + ", ".join(broke))
                entry["broke"] = broke
            if fixed:
                print("  fixed  " + ", ".join(fixed))
                entry["fixed"] = fixed
            if not broke and not fixed:
                print("  no per-case changes since the previous run")
        report.append(entry)

    if args.format == "json":
        print(json.dumps(report, indent=2))
    print()
    if drifted:
        print(f"DRIFT FLAGGED: {len(drifted)}")
        for d in drifted:
            print("  - " + d)
        return 1 if args.fail_on_drift else 0
    print("No drift flagged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
