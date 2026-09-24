#!/usr/bin/env python3
"""Tests for scripts/eval_run.py — the offline quality-regression runner.

Runnable with NO live stack (that's the point of --offline). Asserts:
  1. the PURE graders (exact / contains / regex, + judge dispatch) on synthetic
     outputs — importable, side-effect free;
  2. load_cases + validate_case accept well-formed cases and reject malformed ones;
  3. every committed eval/datasets/starter/*.json is well-formed (required keys,
     grader.type in the allowed set, judge has a goal / others have a value);
  4. --offline mode grades a synthetic outputs file correctly AND exits NON-ZERO
     when any case fails (the CI quality gate).

    python tests/eval_run_test.py     # exits non-zero on any failure
"""
import os, sys, json, tempfile, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
RUNNER = os.path.join(SCRIPTS, "eval_run.py")
sys.path.insert(0, SCRIPTS)
import eval_run  # noqa: E402

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


# --------------------------------------------------------------------------- #
print("Pure graders:")
ok, _ = eval_run.grade_exact("pong", {"type": "exact", "value": "pong"})
check("exact match", ok is True)
ok, _ = eval_run.grade_exact("  pong \n", {"type": "exact", "value": "pong"})
check("exact ignores surrounding whitespace", ok is True)
ok, _ = eval_run.grade_exact("pongs", {"type": "exact", "value": "pong"})
check("exact rejects near-miss", ok is False)

ok, _ = eval_run.grade_contains("the answer is Pong.", {"type": "contains", "value": "pong"})
check("contains is case-insensitive", ok is True)
ok, _ = eval_run.grade_contains("nothing here", {"type": "contains", "value": "pong"})
check("contains rejects absent value", ok is False)

ok, _ = eval_run.grade_regex("id=A1B2", {"type": "regex", "value": r"id=[A-Z0-9]+"})
check("regex match", ok is True)
ok, _ = eval_run.grade_regex("x", {"type": "regex", "value": r"^\d+$"})
check("regex non-match", ok is False)
ok, _ = eval_run.grade_regex("x", {"type": "regex", "value": r"([unclosed"})
check("regex bad pattern -> fail, no crash", ok is False)

# judge dispatch: injected judge_fn decides; no judge_fn -> fail closed.
ok, _ = eval_run.grade("out", {"type": "judge", "goal": "g"}, judge_fn=lambda o, g: (True, "ok"))
check("judge via injected fn (pass)", ok is True)
ok, _ = eval_run.grade("out", {"type": "judge", "goal": "g"}, judge_fn=lambda o, g: False)
check("judge via injected fn (bare-bool fail)", ok is False)
ok, _ = eval_run.grade("out", {"type": "judge", "goal": "g"}, judge_fn=None)
check("judge with no judge_fn fails closed", ok is False)
ok, _ = eval_run.grade("out", {"type": "bogus"})
check("unknown grader type -> fail", ok is False)

# The same graders THROUGH grade(), the path a real run takes. The checks above
# call each grader directly, so a dispatch that returned a pass for every
# `contains` case survived this suite (ADR 0020, watched on 2026-09-19).
for gtype, value, hit, miss in (("exact", "pong", "pong", "ping"),
                                ("contains", "pong", "a pong!", "nothing"),
                                ("regex", r"^po+ng$", "pooong", "pang"),
                                ("not_contains", "secret", "clean", "the secret"),
                                ("not_regex", r"\d{4}", "none", "pin 1234")):
    g = {"type": gtype, "value": value}
    check(f"grade() dispatches {gtype}: pass case", eval_run.grade(hit, g)[0] is True)
    check(f"grade() dispatches {gtype}: fail case", eval_run.grade(miss, g)[0] is False)


# --------------------------------------------------------------------------- #
print("Case validation:")
good = {"id": "c1", "scope": "s", "task_type": "plan", "action": "advise",
        "prompt": "p", "grader": {"type": "contains", "value": "x"},
        "source": "example", "created": "2026-07-19"}
check("well-formed case validates", eval_run.validate_case(good) == [])
check("missing key rejected", any("missing" in p for p in eval_run.validate_case({k: v for k, v in good.items() if k != "prompt"})))
check("bad grader type rejected", eval_run.validate_case(dict(good, grader={"type": "nope", "value": "x"})) != [])
check("judge without goal rejected", eval_run.validate_case(dict(good, grader={"type": "judge"})) != [])
check("contains without value rejected", eval_run.validate_case(dict(good, grader={"type": "contains"})) != [])


# --------------------------------------------------------------------------- #
print("Committed starter dataset:")
try:
    starter = eval_run.load_cases(os.path.join(ROOT, "eval", "datasets", "starter"))
    check("starter loads + validates", len(starter) >= 2, f"{len(starter)} cases")
    check("starter graders all in allowed set",
          all(c["grader"]["type"] in eval_run.ALLOWED_GRADERS for c in starter))
    check("starter has a judge case (goal-based)",
          any(c["grader"]["type"] == "judge" and c["grader"].get("goal") for c in starter))
except Exception as e:
    check("starter loads + validates", False, str(e))


# --------------------------------------------------------------------------- #
print("--offline grading + exit codes:")
with tempfile.TemporaryDirectory() as tmp:
    ds = os.path.join(tmp, "syn")
    os.makedirs(ds)
    cases = {
        "e1": {"id": "e1", "scope": "s", "task_type": "transform", "action": "advise",
               "prompt": "say pong", "grader": {"type": "exact", "value": "pong"},
               "source": "example", "created": "2026-07-19"},
        "c1": {"id": "c1", "scope": "s", "task_type": "classify", "action": "advise",
               "prompt": "classify", "grader": {"type": "contains", "value": "positive"},
               "source": "example", "created": "2026-07-19"},
        "r1": {"id": "r1", "scope": "s", "task_type": "extract", "action": "advise",
               "prompt": "extract id", "grader": {"type": "regex", "value": r"id=[0-9]+"},
               "source": "example", "created": "2026-07-19"},
    }
    for cid, c in cases.items():
        with open(os.path.join(ds, cid + ".json"), "w", encoding="utf-8") as f:
            json.dump(c, f)

    def outputs_file(mapping):
        p = os.path.join(tmp, "out.jsonl")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            for cid, out in mapping.items():
                f.write(json.dumps({"case_id": cid, "output": out}) + "\n")
        return p

    def run(outpath):
        return subprocess.run([sys.executable, RUNNER, "--dataset", ds,
                               "--offline", outpath, "--format", "json"],
                              capture_output=True, text=True)

    # All pass -> exit 0.
    good_out = outputs_file({"e1": "pong", "c1": "This is positive overall.", "r1": "found id=42"})
    res = run(good_out)
    check("all-pass: exit 0", res.returncode == 0, f"rc={res.returncode} {res.stderr[:200]}")
    j = json.loads(res.stdout)
    check("all-pass: 3 passed", j.get("passed") == 3 and j.get("failed") == 0, json.dumps(j.get("cases")))
    check("all-pass: regression false", j.get("regression") is False)

    # One fails -> non-zero exit (regression gate).
    bad_out = outputs_file({"e1": "PONGG", "c1": "This is positive overall.", "r1": "found id=42"})
    res2 = run(bad_out)
    check("one-fail: non-zero exit (gate blocks)", res2.returncode != 0, f"rc={res2.returncode}")
    j2 = json.loads(res2.stdout)
    check("one-fail: exactly 1 failed", j2.get("failed") == 1, json.dumps(j2.get("cases")))
    check("one-fail: regression true", j2.get("regression") is True)

    # Missing offline output for a case -> that case FAILS (not silently skipped).
    partial = outputs_file({"e1": "pong", "c1": "positive"})  # r1 absent
    res3 = run(partial)
    check("missing-output: non-zero exit", res3.returncode != 0, f"rc={res3.returncode}")
    j3 = json.loads(res3.stdout)
    r1row = next((c for c in j3.get("cases", []) if c["id"] == "r1"), {})
    check("missing-output: r1 marked failed", r1row.get("passed") is False, json.dumps(r1row))


print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("All eval_run checks passed.")
