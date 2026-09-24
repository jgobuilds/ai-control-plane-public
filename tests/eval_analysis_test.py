#!/usr/bin/env python3
"""Gates for the eval analysis layer: confidence, A/B statistics, drift.

These exist because every one of the three can be wrong in a way that still
LOOKS like a working measurement — a confidence number that is really a constant,
a p-value from an approximation that does not hold at n=4, a drift check that
never fires. Each assertion below is written to fail if the mechanism degrades
into decoration.

    python tests/eval_analysis_test.py
"""
import importlib.util, io, json, os, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mod(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ROOT, "scripts", name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ER = _mod("eval_run")
AB = _mod("eval_ab")

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


# --------------------------------------------------------------------------- #
print("Exact McNemar (the A/B statistic):")
# Hand-computable values. A normal approximation would disagree with all of
# these at these counts, which is exactly why the exact test is used.
check("no discordant pairs => p = 1", AB.exact_mcnemar(0, 0) == 1.0)
check("1-0 split is not significant", AB.exact_mcnemar(1, 0) == 1.0,
      str(AB.exact_mcnemar(1, 0)))
check("5-0 split: p = 2/32", abs(AB.exact_mcnemar(5, 0) - 0.0625) < 1e-9,
      str(AB.exact_mcnemar(5, 0)))
check("6-0 split: p = 2/64 (first conclusive count)",
      abs(AB.exact_mcnemar(6, 0) - 0.03125) < 1e-9, str(AB.exact_mcnemar(6, 0)))
check("10-0 split: p = 2/1024", abs(AB.exact_mcnemar(10, 0) - 0.001953125) < 1e-9)
check("symmetric in its arguments", AB.exact_mcnemar(7, 2) == AB.exact_mcnemar(2, 7))
check("an even split is never significant", AB.exact_mcnemar(3, 3) == 1.0)
check("p never exceeds 1", all(AB.exact_mcnemar(i, j) <= 1.0
                              for i in range(6) for j in range(6)))
# The floor must be consistent with the statistic: below MIN_DISCORDANT, no
# split can reach p < 0.05, so the floor cannot be hiding real findings.
worst = min(AB.exact_mcnemar(k, 0) for k in range(1, AB.MIN_DISCORDANT))
check("MIN_DISCORDANT is not masking significant results", worst >= 0.05,
      f"a {AB.MIN_DISCORDANT - 1}-0 split reaches p={worst}")

# --------------------------------------------------------------------------- #
print("\nJudge-agreement confidence:")
GRADER = {"type": "judge", "goal": "g"}


def votes_fn(seq):
    """A judge that returns a fixed sequence of verdicts."""
    it = iter(seq)
    return lambda output, goal: (next(it), "because")


p, d, c = ER.grade_judge_votes("x", GRADER, votes_fn([True, True, True]), 3)
check("unanimous pass => passed, confidence 1.0", p is True and c == 1.0, f"{p} {c}")
p, d, c = ER.grade_judge_votes("x", GRADER, votes_fn([True, True, False]), 3)
check("2-1 pass => passed, confidence 0.667", p is True and abs(c - 0.6667) < 1e-3, f"{p} {c}")
check("the dissenting reason is surfaced, not dropped", "dissent" in d, d)
p, d, c = ER.grade_judge_votes("x", GRADER, votes_fn([False, True, False]), 3)
check("2-1 fail => failed, confidence 0.667", p is False and abs(c - 0.6667) < 1e-3, f"{p} {c}")
p, d, c = ER.grade_judge_votes("x", GRADER, votes_fn([True, False]), 2)
check("an even split FAILS (a gate must not pass on a coin flip)", p is False, f"{p} {c}")
check("no judge_fn => no confidence, not a fake 1.0",
      ER.grade_judge_votes("x", GRADER, None, 3)[2] is None)
# The property that makes this a measurement rather than a constant: a divided
# panel must never report full confidence.
check("a divided panel never reports confidence 1.0",
      all(ER.grade_judge_votes("x", GRADER, votes_fn(v), 3)[2] < 1.0
          for v in ([True, True, False], [False, False, True])))

# --------------------------------------------------------------------------- #
print("\nSingle-judge runs report UNKNOWN confidence, not certainty:")
cases = [{"id": "c1", "scope": "s", "task_type": "t", "action": "advise", "prompt": "p",
          "grader": {"type": "judge", "goal": "g"}, "source": "test", "created": "2026-01-01"},
         {"id": "c2", "scope": "s", "task_type": "t", "action": "advise", "prompt": "p",
          "grader": {"type": "contains", "value": "ok"}, "source": "test", "created": "2026-01-01"}]
res = ER.run_eval(cases, offline={"c1": "anything", "c2": "ok"})
byid = {r["id"]: r for r in res["cases"]}
check("deterministic grader is confidence 1.0", byid["c2"]["confidence"] == 1.0)
check("single judge is confidence None, not 1.0", byid["c1"]["confidence"] is None,
      repr(byid["c1"]["confidence"]))

# --------------------------------------------------------------------------- #
print("\nResample: AGENT stability, which judge-votes does not measure:")
# --judge-votes asks whether the GRADERS agree about one output. This asks
# whether the AGENT produces a gradeable output twice running. Conflating them
# means an A/B charges run-to-run noise to whichever variant drew the bad sample.
for label, passes, want_v, want_s in (
        ("unanimous pass", [True] * 5, True, 1.0),
        ("unanimous fail", [False] * 5, False, 1.0),
        ("3 of 5 -> passes, but flagged", [True, True, True, False, False], True, 0.6),
        ("2 of 5 -> fails", [True, True, False, False, False], False, 0.6),
        ("even split fails", [True, False], False, 0.5)):
    v, s = ER.resample_stats(passes)
    check(f"resample {label}", v is want_v and abs(s - want_s) < 1e-9, f"{v} {s}")
check("no runs => fails rather than passing on nothing",
      ER.resample_stats([])[0] is False)
# The property that makes this a measurement: a case that flapped can never
# report full stability, whichever way the majority fell.
check("a flapping case never reports stability 1.0",
      all(ER.resample_stats(p)[1] < 1.0
          for p in ([True, True, False], [False, False, True], [True, False])))
# And a stable case must not be labelled unstable — false variance would send
# someone hunting a nondeterminism that is not there.
check("a deterministic case reports stability 1.0",
      ER.resample_stats([True, True, True])[1] == 1.0
      and ER.resample_stats([False, False])[1] == 1.0)

print("\nNegative graders cannot pass vacuously:")
# The bug this pins, caught on the first live run of a curated dataset: the
# router returned an empty result (both vendor CLIs were failing) and
# `not_regex: no currency figure` reported PASS. The case was satisfied by the
# model saying nothing. Absence of a forbidden string is trivially true of the
# empty string, so every negative assertion had this hole.
for gtype, g in (("not_regex", {"type": "not_regex", "value": r"\$[\d,]+"}),
                 ("not_contains", {"type": "not_contains", "value": "secret"})):
    for label, out in (("empty", ""), ("whitespace", "  \n\t ")):
        ok, detail = ER.grade(out, g)
        check(f"{gtype} FAILS on {label} output", ok is False, detail)
check("not_regex still passes on genuinely clean output",
      ER.grade("nothing to see", {"type": "not_regex", "value": r"\$[\d,]+"})[0] is True)
check("not_regex still fails on a real leak",
      ER.grade("we billed $250,000", {"type": "not_regex", "value": r"\$[\d,]+"})[0] is False)
check("not_contains still passes when the string is absent",
      ER.grade("all clear", {"type": "not_contains", "value": "secret"})[0] is True)
check("not_contains still fails when it leaks",
      ER.grade("the secret is out", {"type": "not_contains", "value": "secret"})[0] is False)
check("a bad regex fails closed rather than passing",
      ER.grade("x", {"type": "not_regex", "value": "([unclosed"})[0] is False)

print("\nResults ledger + drift detection:")
tmp = tempfile.mkdtemp()
ledger = os.path.join(tmp, "results.jsonl")


def write_run(pass_rate, by_case, ts):
    rec = {"ts": ts, "sha": "deadbee", "dataset": "d", "label": "baseline",
           "overrides": {}, "mode": "offline", "total": len(by_case),
           "passed": sum(1 for v in by_case.values() if v),
           "failed": sum(1 for v in by_case.values() if not v),
           "passRate": pass_rate, "meanConfidence": 1.0, "byCase": by_case}
    with io.open(ledger, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def history(*extra):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "eval_history.py"),
                        "--results", ledger, *extra],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


code, out = history()
check("an empty ledger is not an error", code == 0 and "No eval results" in out, out[:120])

for i, ts in enumerate(["2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00",
                        "2026-01-03T00:00:00+00:00"]):
    write_run(1.0, {"a": True, "b": True, "c": True}, ts)
code, out = history("--fail-on-drift")
check("a steady suite flags no drift", code == 0 and "No drift flagged" in out, out[-200:])

write_run(0.3333, {"a": True, "b": False, "c": False}, "2026-01-04T00:00:00+00:00")
code, out = history("--fail-on-drift")
check("a real quality drop IS flagged", code == 1 and "DRIFT" in out, out[-200:])
check("--fail-on-drift exits non-zero", code == 1)
check("the specific broken cases are named", "BROKE" in out and "b" in out, out[-200:])

# The failure this catches: aggregate pass rate holding while the suite churns
# underneath it. Two cases swap verdicts, the rate is identical, and only the
# per-case view can see it.
ledger = os.path.join(tmp, "churn.jsonl")
write_run(0.5, {"a": True, "b": False}, "2026-02-01T00:00:00+00:00")
write_run(0.5, {"a": False, "b": True}, "2026-02-02T00:00:00+00:00")
code, out = history()
check("a flat pass rate still surfaces per-case churn",
      "BROKE" in out and "fixed" in out, out[-300:])

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All eval-analysis checks passed.")
