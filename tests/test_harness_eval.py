#!/usr/bin/env python3
"""Corpus for the harness eval — especially its refusal to rank thin data.

The whole point of this feature is that a run's outcome comes from a
CONFIGURATION, not from a model in isolation. The failure mode it must not have
is the one it would be most useful for: presenting a confident per-config
ranking built from three runs. So the tests below care as much about what it
declines to say as about what it computes.

    python tests/test_harness_eval.py
"""
import os, sys, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "eval_metrics", os.path.join(HERE, "..", "scripts", "eval_metrics.py"))
em = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(em)

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def rec(tier="t2", model="claude-sonnet-5", provider="claude", mode="human-led",
        verdict=None, blocked=None, cost=0.01, dur=1000):
    r = {"tier": tier, "model": model, "provider": provider, "mode": mode,
         "usage": {"notionalCostUsd": cost, "durationMs": dur}}
    if verdict is not None:
        r["verdict"] = verdict
    if blocked is not None:
        r["blocked"] = blocked
    return r


print("Harness signature — configuration, not just the model:")
a = em.harness_signature(rec(tier="t2", verdict=True))
b = em.harness_signature(rec(tier="t2"))
check("verify vs no-verify are DIFFERENT harnesses on the same model", a != b,
      f"{a!r} == {b!r}")
c = em.harness_signature(rec(tier="t2", mode="human-led"))
d = em.harness_signature(rec(tier="t2", mode="ai-led"))
check("operating mode changes the signature", c != d)
e1 = em.harness_signature(rec(tier="t2"))
e2 = em.harness_signature(rec(tier="t3"))
check("tier changes the signature", e1 != e2)
check("identical config groups together",
      em.harness_signature(rec()) == em.harness_signature(rec()))

print("\nRefusal to rank thin data (the property that matters):")
thin = [rec(tier="t2"), rec(tier="t3", model="claude-fable-5")]
res = em.harness_breakdown(thin, min_n=5)
check("two 1-run configs are reported", len(res["configs"]) == 2)
check("neither is marked sufficient", all(not c["sufficient"] for c in res["configs"]))
check("nothing is comparable", res["comparable"] == [])
check("the note says NOT ENOUGH DATA", "NOT ENOUGH DATA" in res["note"], res["note"])
check("the note warns against acting on it", "do not" in res["note"].lower())

print("\nOne sufficient config is still not a comparison:")
one = [rec(tier="t2") for _ in range(6)] + [rec(tier="t3")]
res = em.harness_breakdown(one, min_n=5)
check("the n=6 config is sufficient",
      [c for c in res["configs"] if c["n"] == 6][0]["sufficient"])
check("a single sufficient config yields no comparison", len(res["comparable"]) == 1)
check("and still says NOT ENOUGH DATA", "NOT ENOUGH DATA" in res["note"])

print("\nTwo sufficient configs DO compare:")
two = [rec(tier="t2", verdict=True) for _ in range(5)] + \
      [rec(tier="t3", model="claude-fable-5", verdict=False) for _ in range(5)]
res = em.harness_breakdown(two, min_n=5)
check("both comparable", len(res["comparable"]) == 2)
check("note no longer refuses", "NOT ENOUGH DATA" not in res["note"])
check("it still disclaims significance", "not a significance test" in res["note"])

print("\nArithmetic:")
mixed = [rec(verdict=True, cost=1.0) for _ in range(3)] + \
        [rec(verdict=False, cost=1.0) for _ in range(1)]
cfg = em.harness_breakdown(mixed, min_n=1)["configs"][0]
check("verify_pass_pct = 3/4", cfg["verify_pass_pct"] == 75.0, str(cfg["verify_pass_pct"]))
check("quality_held counts only verdict-true here", cfg["quality_held_pct"] == 75.0)
check("cost totals", cfg["notional_cost_usd"] == 4.0, str(cfg["notional_cost_usd"]))
check("cost per HELD outcome, not per run", cfg["cost_per_held_usd"] == round(4.0 / 3, 4),
      str(cfg["cost_per_held_usd"]))

blocked_only = [rec(blocked="halted") for _ in range(3)]
cfg = em.harness_breakdown(blocked_only, min_n=1)["configs"][0]
check("blocked runs counted", cfg["blocked"] == 3)
check("blocked runs never count as quality held", cfg["quality_held_pct"] == 0.0)
check("cost_per_held is None when nothing held (not a divide-by-zero, not 0)",
      cfg["cost_per_held_usd"] is None)

print("\nEmpty input is honest, not silently zero:")
res = em.harness_breakdown([], min_n=5)
check("no configs", res["configs"] == [])
check("still refuses to compare", "NOT ENOUGH DATA" in res["note"])

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All harness-eval checks passed.")
