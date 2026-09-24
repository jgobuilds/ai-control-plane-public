#!/usr/bin/env python3
"""Paired A/B of two routing variants over the SAME eval cases.

WHY PAIRED, NOT A TRAFFIC SPLIT. The obvious design — tag live requests A or B
and compare aggregates — needs volume this stack does not have. A solo operation
produces a handful of decisions a day, so a live split would take months to
distinguish a real effect from noise, and an underpowered A/B that announces a
winner is worse than no A/B: it launders noise into a decision. Running the same
cases through both variants removes between-group variance entirely. Every case
is its own control, the answer arrives in minutes, and the only thing left to
account for is grader non-determinism.

WHAT A VARIANT IS. A set of request overrides merged into each replay —
`{"tier":"t3"}`, `{"model":"..."}`, `{"verify":true}`. The router honours them
only when `policy.controls.allowRequestOverride` is true, so the ability to run
this experiment is something policy grants rather than something this script
takes.

THE STATISTIC. Only DISCORDANT pairs carry information: cases where the variants
disagree. Cases both got right, or both got wrong, say nothing about which is
better. That is McNemar's test, and with counts this small the exact binomial is
the honest version — no normal approximation, no continuity correction, no
pretending n=4 supports a chi-square. If there are too few discordant pairs to
conclude anything, this says so instead of reporting a winner.

    # offline: grade pre-captured outputs from each variant (no stack, CI-safe)
    python scripts/eval_ab.py --dataset eval/datasets/starter \\
        --a-offline out-a.jsonl --b-offline out-b.jsonl

    # live: same cases, two sets of request overrides
    python scripts/eval_ab.py --dataset eval/datasets/starter \\
        --a '{}' --b '{"tier":"t3"}' --a-label t2 --b-label t3

Pure stdlib.
"""
import argparse, importlib.util, json, math, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_eval_run():
    spec = importlib.util.spec_from_file_location(
        "eval_run", os.path.join(ROOT, "scripts", "eval_run.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ER = _load_eval_run()

# Below this many discordant pairs, no split is distinguishable from a coin. The
# exact test already reflects that in its p-value; naming the floor separately
# stops a "p=0.25, B looks better" reading from turning into a decision.
MIN_DISCORDANT = 6


def exact_mcnemar(b, c):
    """Two-sided exact binomial p over discordant pairs (b wins vs c wins).

    Under the null the two variants are equally likely to win any discordant
    pair, so the count is Binomial(n=b+c, p=0.5). Doubling the smaller tail is
    the standard two-sided construction; it can exceed 1, hence the cap.
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) * (0.5 ** n)
    return min(1.0, 2.0 * tail)


def run_variant(cases, offline_path, overrides, router_url, token, votes):
    offline = ER.load_offline(offline_path) if offline_path else None
    return ER.run_eval(cases, offline=offline, router_url=router_url, token=token,
                       overrides=overrides, votes=votes)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Paired A/B over the same eval cases.")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--a", default=None, help='variant A request overrides (JSON)')
    ap.add_argument("--b", default=None, help='variant B request overrides (JSON)')
    ap.add_argument("--a-offline", default=None, help="pre-captured outputs for A")
    ap.add_argument("--b-offline", default=None, help="pre-captured outputs for B")
    ap.add_argument("--a-label", default="A")
    ap.add_argument("--b-label", default="B")
    ap.add_argument("--router-url", default=ER.DEFAULT_ROUTER_URL)
    ap.add_argument("--router-token", default=ER.DEFAULT_ROUTER_TOKEN)
    ap.add_argument("--judge-votes", type=int, default=1)
    ap.add_argument("--record", action="store_true", help="append both runs to the ledger")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args(argv)

    def parse(js, which):
        if not js:
            return None
        try:
            v = json.loads(js)
            if not isinstance(v, dict):
                raise ValueError("must be a JSON object")
            return v
        except (ValueError, json.JSONDecodeError) as e:
            print(f"eval_ab: bad --{which}: {e}", file=sys.stderr)
            raise SystemExit(2)

    ov_a, ov_b = parse(args.a, "a"), parse(args.b, "b")
    if ov_a is not None and ov_b is not None and ov_a == ov_b and not (
            args.a_offline or args.b_offline):
        # Identical variants measure grader non-determinism, not a difference.
        # That is a legitimate thing to measure, but it is not what the flags say.
        print("eval_ab: WARNING --a and --b are identical; this measures grader "
              "noise, not a variant difference.", file=sys.stderr)

    try:
        cases = ER.load_cases(args.dataset)
    except ValueError as e:
        print(f"eval_ab: {e}", file=sys.stderr)
        return 2

    res_a = run_variant(cases, args.a_offline, ov_a, args.router_url,
                        args.router_token, args.judge_votes)
    res_b = run_variant(cases, args.b_offline, ov_b, args.router_url,
                        args.router_token, args.judge_votes)

    pa = {r["id"]: r["passed"] for r in res_a["cases"]}
    pb = {r["id"]: r["passed"] for r in res_b["cases"]}
    ids = [c["id"] for c in cases]
    both_pass = [i for i in ids if pa.get(i) and pb.get(i)]
    both_fail = [i for i in ids if not pa.get(i) and not pb.get(i)]
    a_only = [i for i in ids if pa.get(i) and not pb.get(i)]
    b_only = [i for i in ids if pb.get(i) and not pa.get(i)]
    p = exact_mcnemar(len(a_only), len(b_only))
    discordant = len(a_only) + len(b_only)

    out = {
        "dataset": os.path.basename(str(args.dataset).rstrip("/\\")),
        "a": {"label": args.a_label, "overrides": ov_a or {}, "passRate": res_a["passRate"]},
        "b": {"label": args.b_label, "overrides": ov_b or {}, "passRate": res_b["passRate"]},
        "bothPass": len(both_pass), "bothFail": len(both_fail),
        "aOnly": a_only, "bOnly": b_only,
        "discordant": discordant, "pExact": round(p, 4),
        "conclusive": discordant >= MIN_DISCORDANT and p < 0.05,
    }

    if args.format == "json":
        print(json.dumps(out, indent=2))
    else:
        print(f"\nPaired A/B — {out['dataset']}   {len(ids)} case(s)")
        print(f"  {args.a_label:<12} pass {ER_pct(res_a['passRate'])}   overrides={ov_a or {}}")
        print(f"  {args.b_label:<12} pass {ER_pct(res_b['passRate'])}   overrides={ov_b or {}}")
        print(f"\n  both pass : {len(both_pass)}")
        print(f"  both fail : {len(both_fail)}")
        print(f"  {args.a_label} only   : {len(a_only)}" + (f"  {a_only}" if a_only else ""))
        print(f"  {args.b_label} only   : {len(b_only)}" + (f"  {b_only}" if b_only else ""))
        print(f"\n  discordant pairs: {discordant}   exact two-sided p = {p:.4f}")
        if discordant < MIN_DISCORDANT:
            print(f"  NOT CONCLUSIVE — fewer than {MIN_DISCORDANT} discordant pairs. "
                  "Only cases where the variants DISAGREE carry information, and "
                  "there are not enough of them to tell a difference from a coin. "
                  "Add cases; do not read the winner off the pass rates.")
        elif p < 0.05:
            better = args.a_label if len(a_only) > len(b_only) else args.b_label
            print(f"  {better} wins (p < 0.05 on {discordant} discordant pairs).")
        else:
            print("  No significant difference. The variants disagree, but not "
                  "consistently enough in one direction.")
        if res_a["mode"] == "live-replay" and any(
                c["grader"].get("type") == "judge" for c in cases) and args.judge_votes == 1:
            print("\n  NOTE: judge-graded cases with --judge-votes=1. A single "
                  "non-deterministic grader can flip a case on its own, which "
                  "shows up here as a discordant pair that is really grader noise. "
                  "Use --judge-votes 3.")

    if args.record:
        ER.record_result(res_a, args.dataset, label=args.a_label, overrides=ov_a)
        ER.record_result(res_b, args.dataset, label=args.b_label, overrides=ov_b)
        print(f"\n  recorded both runs -> {ER.RESULTS_PATH}")
    return 0


def ER_pct(x):
    return "  n/a" if x is None else f"{x * 100:5.1f}%"


if __name__ == "__main__":
    sys.exit(main())
