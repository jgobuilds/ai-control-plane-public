#!/usr/bin/env python3
"""Offline eval runner — the QUALITY-regression counterpart to the eval/drift lane.

Where scripts/eval_metrics.py measures *metadata the router already logged* (a
proxy — see EVAL.md), THIS runs a real semantic/ground-truth eval: it takes a set
of regression CASES (captured from production failures + curated examples), REPLAYS
each through the router, and GRADES the output against an expected value / pattern /
goal. It is the offline quality gate that closes the semantic-eval gap EVAL.md flags.

Adapted from LangChain's "operationalize the agent improvement loop" (production
trace -> capture -> offline eval against a regression dataset -> fix -> prevent
recurrence), done the the control plane way: governed observability — eval data stays
per-scope, PII-tokenized via the scrubber, retention-bound, local. See
IMPROVEMENT-LOOP.md.

A case (eval/datasets/<name>/<id>.json):
    { "id", "scope", "task_type", "action", "prompt",
      "grader": { "type": "exact|contains|regex|judge", "value"?: "...", "goal"?: "..." },
      "source", "created" }

Two run modes:
    # live replay through the router (needs the stack; see the note below)
    python scripts/eval_run.py --dataset eval/datasets/starter
    # offline: grade PRE-CAPTURED outputs (CI-runnable, no live stack)
    python scripts/eval_run.py --dataset eval/datasets/starter --offline outputs.jsonl

Exit code is NON-ZERO on any regression (a failing case), so it is a CI gate.

LIVE REPLAY NOTE: the router has no host port (docker-compose exposes nothing), so
live replay runs from INSIDE the compose network — e.g.
    docker compose exec router python /app/scripts/eval_run.py --dataset ...
with ROUTER_URL=http://localhost:8080 inside the container — or against a mock.
CI and the committed test use --offline, which needs no router at all.

Pure stdlib (urllib). Windows-friendly.
"""
import sys, os, json, re, argparse, urllib.request, urllib.error

ALLOWED_GRADERS = ("exact", "contains", "regex", "not_contains", "not_regex", "judge")

DEFAULT_ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8080")
DEFAULT_ROUTER_TOKEN = os.environ.get("ROUTER_TOKEN", "")

REQUIRED_CASE_KEYS = ("id", "scope", "task_type", "action", "prompt", "grader", "source", "created")


# --------------------------------------------------------------------------- #
# PURE GRADERS. Each takes (output, grader) and returns (passed: bool, detail: str).
# They are importable and side-effect free so they can be unit-tested directly and
# reused by any caller (the n8n feedback lane, a future online-eval sampler, etc.).
def grade_exact(output, grader):
    want = str(grader.get("value", ""))
    got = str(output)
    ok = got.strip() == want.strip()
    return ok, f"exact: {'==' if ok else '!='} expected {want!r}"


def grade_contains(output, grader):
    """Case-insensitive substring match — robust to trivial capitalization drift."""
    want = str(grader.get("value", ""))
    ok = want.strip().lower() in str(output).lower()
    return ok, f"contains: {want!r} {'found' if ok else 'NOT found'}"


def grade_regex(output, grader):
    pat = str(grader.get("value", ""))
    try:
        ok = re.search(pat, str(output)) is not None
    except re.error as e:
        return False, f"regex: bad pattern {pat!r}: {e}"
    return ok, f"regex: /{pat}/ {'matched' if ok else 'did NOT match'}"


def _empty_fails(output, gtype):
    """Negative graders must FAIL on empty output. Returns a failure tuple or None.

    Caught on the first live run of a curated dataset: the router returned an
    empty result and `not_regex: no currency figure` reported PASS — the case was
    satisfied by the model saying nothing at all. Every negative assertion has
    this hole, because absence of a forbidden string is trivially true of the
    empty string.

    Silence is not compliance. A case that can be satisfied by producing nothing
    measures nothing, which is the same vacuity that made a green audit-ledger
    proof and a green mount check meaningless elsewhere in this repo.
    """
    if not str(output or "").strip():
        return False, (f"{gtype}: output was EMPTY — a negative assertion is "
                       "trivially true of nothing, so this FAILS rather than "
                       "passing vacuously")
    return None


def grade_not_contains(output, grader):
    """Passes when the substring is ABSENT.

    Most safety regressions are shaped this way — did not invent a figure, did
    not name a client, did not emit a raw address — and every other grader here
    is a positive match, so those cases had only the judge, which is
    non-deterministic. A rule worth gating on deserves a deterministic grader
    when the forbidden string is knowable, and usually it is.
    """
    empty = _empty_fails(output, "not_contains")
    if empty:
        return empty
    want = str(grader.get("value", ""))
    ok = want.strip().lower() not in str(output).lower()
    return ok, f"not_contains: {want!r} {'absent (good)' if ok else 'PRESENT (leaked)'}"


def grade_not_regex(output, grader):
    """Passes when the pattern does NOT match. The general form of the above —
    for shapes rather than literals, e.g. a currency figure or an email."""
    empty = _empty_fails(output, "not_regex")
    if empty:
        return empty
    pat = str(grader.get("value", ""))
    try:
        m = re.search(pat, str(output))
    except re.error as e:
        return False, f"not_regex: bad pattern {pat!r}: {e}"
    return m is None, (f"not_regex: /{pat}/ absent (good)" if m is None
                       else f"not_regex: /{pat}/ MATCHED {m.group(0)!r}")


def grade_judge(output, grader, judge_fn=None):
    """LLM-as-judge — delegate the pass/fail decision to `judge_fn(output, goal)`.

    judge_fn returns (passed: bool, detail: str) or a bare bool. It is injected so
    the pure grader stays testable; the live implementation (make_router_judge)
    calls the router again with a task_type:review grading prompt (the verifier
    pattern). NON-DETERMINISTIC by nature — see the honest-limits note in the docs."""
    goal = str(grader.get("goal", "")) or "(judge for correctness and completeness)"
    if judge_fn is None:
        return False, "judge: no judge_fn available (offline without a judge, or router unset)"
    res = judge_fn(output, goal)
    if isinstance(res, tuple):
        ok, detail = res
    else:
        ok, detail = bool(res), ""
    return bool(ok), f"judge: goal={goal!r} -> {'PASS' if ok else 'FAIL'}" + (f" ({detail})" if detail else "")


def grade_judge_votes(output, grader, judge_fn, votes):
    """Run N independent judges; return (passed, detail, confidence).

    CONFIDENCE IS AGREEMENT, and that choice is the whole point. The obvious
    alternative — asking the model how confident it is — produces a number that
    is uncorrelated with correctness and LOOKS like a measurement, which is worse
    than having none. Independent judges disagreeing is an observation about the
    output: if three fresh-context graders split 2-1, the case is genuinely
    ambiguous against its stated goal, and that is worth surfacing whichever way
    the majority fell.

    Majority decides pass/fail; confidence is the majority fraction, so 1.0 means
    unanimous and 0.67 means one dissenter out of three. Ties (an even `votes`
    split down the middle) resolve to FAIL: an eval gate that passes on a coin
    flip is not a gate. Use an odd number.

    A judge that errors returns False from make_router_judge, so transport
    failures count as dissent rather than being silently dropped — N-1 usable
    votes reported as unanimous would overstate confidence exactly when the
    system is least healthy.
    """
    goal = str(grader.get("goal", "")) or "(judge for correctness and completeness)"
    if judge_fn is None:
        return False, "judge: no judge_fn available", None
    results = []
    for _ in range(votes):
        res = judge_fn(output, goal)
        ok, detail = res if isinstance(res, tuple) else (bool(res), "")
        results.append((bool(ok), detail))
    yes = sum(1 for ok, _ in results if ok)
    passed = yes * 2 > votes                      # strict majority; a tie fails
    confidence = round(max(yes, votes - yes) / votes, 4)
    dissent = [d for ok, d in results if ok != passed and d]
    detail = (f"judge x{votes}: {yes}/{votes} pass -> {'PASS' if passed else 'FAIL'} "
              f"(confidence {confidence})")
    if dissent:
        detail += " | dissent: " + "; ".join(dissent[:2])[:200]
    return passed, detail, confidence


def grade(output, grader, judge_fn=None):
    """Dispatch to the right grader. Returns (passed, detail)."""
    gtype = (grader or {}).get("type")
    if gtype == "exact":
        return grade_exact(output, grader)
    if gtype == "contains":
        return grade_contains(output, grader)
    if gtype == "regex":
        return grade_regex(output, grader)
    if gtype == "not_contains":
        return grade_not_contains(output, grader)
    if gtype == "not_regex":
        return grade_not_regex(output, grader)
    if gtype == "judge":
        return grade_judge(output, grader, judge_fn)
    return False, f"unknown grader type {gtype!r} (allowed: {', '.join(ALLOWED_GRADERS)})"


# --------------------------------------------------------------------------- #
# CASE LOADING + VALIDATION.
def validate_case(case, where=""):
    """Return a list of problems (empty == well-formed)."""
    problems = []
    if not isinstance(case, dict):
        return [f"{where}: not a JSON object"]
    for k in REQUIRED_CASE_KEYS:
        if k not in case:
            problems.append(f"{where}: missing key {k!r}")
    g = case.get("grader")
    if not isinstance(g, dict):
        problems.append(f"{where}: grader must be an object")
    else:
        gt = g.get("type")
        if gt not in ALLOWED_GRADERS:
            problems.append(f"{where}: grader.type {gt!r} not in {ALLOWED_GRADERS}")
        elif gt == "judge":
            if not g.get("goal"):
                problems.append(f"{where}: judge grader needs a non-empty grader.goal")
        else:
            if g.get("value") in (None, ""):
                problems.append(f"{where}: {gt} grader needs a non-empty grader.value")
    return problems


def load_cases(dataset):
    """Load + validate every *.json case under a dataset dir (or a datasets/<name>).

    `dataset` may be a path to a directory OR a bare dataset name resolved under
    <repo>/eval/datasets/<name>. Raises ValueError on any malformed case (fail loud
    — a broken regression suite must not silently pass)."""
    path = dataset
    if not os.path.isdir(path):
        path = os.path.join(repo_root(), "eval", "datasets", dataset)
    if not os.path.isdir(path):
        raise ValueError(f"dataset not found: {dataset!r} (looked at {path})")
    cases, problems, ids = [], [], set()
    for name in sorted(os.listdir(path)):
        if not name.endswith(".json"):
            continue
        fp = os.path.join(path, name)
        try:
            with open(fp, encoding="utf-8") as f:
                case = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            problems.append(f"{name}: unreadable ({e})")
            continue
        errs = validate_case(case, where=name)
        problems.extend(errs)
        if errs:
            continue
        cid = case["id"]
        if cid in ids:
            problems.append(f"{name}: duplicate case id {cid!r}")
        ids.add(cid)
        case["_file"] = fp
        cases.append(case)
    if problems:
        raise ValueError("malformed dataset:\n  - " + "\n  - ".join(problems))
    return cases


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------- #
# ROUTER I/O (live replay + the judge). urllib only.
def post_json(url, payload, headers, timeout=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_output(resp):
    """Mirror the router's textOf(): pull the runner text out of a /route response.

    Returns (text, blocked_reason). A blocked/errored decision yields ("", reason)."""
    if not isinstance(resp, dict):
        return str(resp), None
    if resp.get("blocked"):
        return "", str(resp.get("blocked"))
    if resp.get("error"):
        return "", "error:" + str(resp.get("error"))
    r = resp.get("result")
    if isinstance(r, dict):
        return str(r.get("result") if r.get("result") is not None
                   else (r.get("text") if r.get("text") is not None else json.dumps(r))), None
    return ("" if r is None else str(r)), None


def replay(case, router_url, token, overrides=None):
    """Replay one case through POST /route. Returns (output_text, error_or_None).

    `overrides` merge into the payload and are what makes an A/B possible at all:
    a variant IS a set of request overrides (tier, model, provider, verify), so
    the same case can be run two ways without editing the dataset. The router
    honours them only when policy.controls.allowRequestOverride is true — which
    is correct, and means an A/B over tiers is a thing the policy grants, not
    something this script can take.
    """
    payload = {
        "prompt": case["prompt"],
        "scope": case["scope"],
        "task_type": case.get("task_type"),
        "action": case.get("action", "advise"),
        "trigger": "turn",
    }
    if overrides:
        payload.update(overrides)
    try:
        resp = post_json(router_url.rstrip("/") + "/route", payload,
                         {"x-router-token": token}, timeout=16 * 60)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return "", f"router HTTP {e.code}: {body[:200]}"
    except (urllib.error.URLError, OSError) as e:
        return "", f"router unreachable: {e}"
    text, blocked = extract_output(resp)
    if blocked:
        return "", f"blocked:{blocked}"
    return text, None


def make_router_judge(router_url, token, scope):
    """Build a judge_fn(output, goal)->(bool,detail) that asks the router (task_type
    review) for a pass/fail. Reuses the verifier's strict-reviewer JSON contract."""
    def judge_fn(output, goal):
        review_prompt = "\n".join([
            "You are a strict, independent grader with no prior context.",
            "Judge ONLY whether the WORK satisfies the GOAL. Do not fix it.",
            'Reply with one JSON object: { "pass": true|false, "reasons": ["..."] }',
            "",
            f"GOAL:\n{goal}",
            "",
            f"WORK:\n{output}",
        ])
        try:
            resp = post_json(router_url.rstrip("/") + "/route",
                             {"prompt": review_prompt, "scope": scope,
                              "task_type": "review", "action": "advise", "trigger": "turn"},
                             {"x-router-token": token}, timeout=16 * 60)
        except Exception as e:  # judge is best-effort; a transport error is a FAIL
            return False, f"judge transport error: {e}"
        text, blocked = extract_output(resp)
        if blocked:
            return False, f"judge blocked:{blocked}"
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return False, "judge returned no JSON verdict"
        try:
            verdict = json.loads(m.group(0))
        except json.JSONDecodeError:
            return False, "judge verdict was not valid JSON"
        reasons = "; ".join(verdict.get("reasons", []) or [])
        return bool(verdict.get("pass")), reasons
    return judge_fn


# --------------------------------------------------------------------------- #
# RESULTS LEDGER — the thing that turns a pass/fail moment into a time series.
#
# Every eval run used to print a verdict and vanish, so "is quality drifting?"
# had no data behind it: the online lane trends metadata from the audit ledger,
# and the semantic half trended nothing at all. One append per run fixes that and
# is the prerequisite for A/B comparison and for semantic drift detection.
#
# NOT the audit ledger, and deliberately not hash-chained. The audit ledger is
# tamper-evident because it records governance decisions someone may later need
# to prove; this records measurements of our own test suite. Borrowing the chain
# would imply a threat model that does not apply here.
RESULTS_PATH = os.environ.get(
    "EVAL_RESULTS", os.path.join(repo_root(), "eval", "results.jsonl"))


def git_sha():
    """Best-effort commit id, so a result can be tied to the code that produced it."""
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo_root(),
                              capture_output=True, text=True, check=True,
                              encoding="utf-8").stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def record_result(res, dataset, label=None, overrides=None, path=None):
    """Append one run to the results ledger. Returns the record written."""
    import datetime
    rec = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "sha": git_sha(),
        "dataset": os.path.basename(str(dataset).rstrip("/\\")),
        "label": label or "default",
        "overrides": overrides or {},
        "mode": res.get("mode"),
        "total": res.get("total"),
        "passed": res.get("passed"),
        "failed": res.get("failed"),
        "passRate": res.get("passRate"),
        "meanConfidence": res.get("meanConfidence"),
        # Per-case pass map, not the outputs. Outputs can carry scope content;
        # this file is measurement data and has no business holding it.
        "byCase": {r["id"]: bool(r["passed"]) for r in res.get("cases", [])},
    }
    p = path or RESULTS_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    return rec


# --------------------------------------------------------------------------- #
# OFFLINE outputs.
def load_offline(path):
    """Read {case_id, output} lines from a .jsonl into a {case_id: output} map."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            cid = rec.get("case_id") or rec.get("id")
            if cid is None:
                raise ValueError(f"{path}:{i}: line missing case_id")
            out[cid] = rec.get("output", "")
    return out


# --------------------------------------------------------------------------- #
def resample_stats(passes):
    """Agent variance across N runs of the SAME case. Returns (verdict, stability).

    `--judge-votes` measures whether the GRADERS agree about one output.
    This measures whether the AGENT produces a gradeable output at all, twice
    running. They are different failures and were being conflated: a case that
    passes 3 times out of 5 is not a case that "passes", and an A/B comparing two
    variants would have charged that noise to the variant.

    stability = the majority share, so 1.0 is deterministic and 0.6 is 3-of-5.
    The verdict is the MAJORITY, and a flapping case is reported rather than
    silently rounded — an unstable pass is a finding about the case (or the
    prompt), not a result.
    """
    n = len(passes)
    if not n:
        return False, None
    yes = sum(1 for p in passes if p)
    verdict = yes * 2 > n            # strict majority; an even split fails
    return verdict, round(max(yes, n - yes) / n, 4)


def run_eval(cases, offline=None, router_url=None, token=None, overrides=None, votes=1,
             resample=1):
    """Grade every case. offline: {case_id: output} to grade instead of replaying.
    Returns a result dict with per-case rows + a summary.

    `votes` > 1 applies ONLY to judge graders and is the confidence mechanism:
    run N independent judges and report their agreement. See grade_judge_votes.
    Deterministic graders (exact/contains/regex) ignore it — running a regex
    three times and calling the unanimity "confidence" would be theatre.
    """
    rows = []
    # Resampling only means anything on a LIVE replay. Grading one captured
    # output five times measures nothing and would bill five judge calls to say
    # so, which is why this clamps rather than trusting the flag.
    runs = max(1, int(resample)) if offline is None else 1
    for case in cases:
        cid = case["id"]
        grader = case["grader"]
        judge_fn = None
        error = None
        attempts = []
        for _ in range(runs):
            if offline is not None:
                if cid not in offline:
                    output, error = "", "no offline output for this case"
                else:
                    output, error = offline[cid], None
                if grader.get("type") == "judge" and router_url:
                    judge_fn = make_router_judge(router_url, token, case["scope"])
            else:
                output, error = replay(case, router_url, token, overrides=overrides)
                if grader.get("type") == "judge":
                    judge_fn = make_router_judge(router_url, token, case["scope"])
            if error:
                attempts.append((False, error, None))
                continue
            if grader.get("type") == "judge" and votes > 1:
                attempts.append(grade_judge_votes(output, grader, judge_fn, votes))
            else:
                p, d = grade(output, grader, judge_fn=judge_fn)
                # A deterministic grader is certain by construction. A single
                # judge is NOT — but reporting 1.0 for it would erase the
                # distinction the whole feature exists to draw, so it reports
                # null instead.
                c = (1.0 if grader.get("type") in
                     ("exact", "contains", "regex", "not_contains", "not_regex") else None)
                attempts.append((p, d, c))

        passes = [bool(a[0]) for a in attempts]
        if runs > 1:
            passed, stability = resample_stats(passes)
            detail = (f"resampled x{runs}: {sum(passes)}/{runs} passed "
                      f"(stability {stability})"
                      + ("  ← FLAPPING" if stability < 1.0 else ""))
            # The last attempt's detail still carries the grader's reasoning,
            # which is what a human reads when a case flaps.
            detail += f" | last: {str(attempts[-1][1])[:160]}"
        else:
            passed, detail, stability = attempts[0][0], attempts[0][1], None
        confidences = [a[2] for a in attempts if a[2] is not None]
        rows.append({"id": cid, "passed": bool(passed), "detail": detail,
                     "grader": grader.get("type"), "scope": case["scope"],
                     "confidence": (round(sum(confidences) / len(confidences), 4)
                                    if confidences else None),
                     **({"stability": stability, "runs": runs} if runs > 1 else {})})
    passed = sum(1 for r in rows if r["passed"])
    failed = len(rows) - passed
    graded = [r["confidence"] for r in rows if r.get("confidence") is not None]
    return {
        "total": len(rows),
        "passed": passed,
        "failed": failed,
        "regression": failed > 0,
        "mode": "offline" if offline is not None else "live-replay",
        "passRate": round(passed / len(rows), 4) if rows else None,
        "meanConfidence": round(sum(graded) / len(graded), 4) if graded else None,
        "lowConfidence": [r["id"] for r in rows
                          if r.get("confidence") is not None and r["confidence"] < 1.0],
        # Cases whose verdict changed between identical runs. These are the ones
        # to fix before trusting any A/B: a flapping case charges its own noise
        # to whichever variant happened to draw the bad sample.
        "unstable": [r["id"] for r in rows
                     if r.get("stability") is not None and r["stability"] < 1.0],
        "cases": rows,
    }


def render_text(res):
    L = ["Offline eval — quality regression against the dataset",
         f"  mode: {res['mode']}   cases: {res['total']}   "
         f"passed: {res['passed']}   failed: {res['failed']}", ""]
    for r in res["cases"]:
        L.append(f"  {'PASS' if r['passed'] else 'FAIL'}  {r['id']:<28} "
                 f"[{r['grader']}/{r['scope']}]  {r['detail']}")
    L.append("")
    if res["regression"]:
        L.append(f"  REGRESSION: {res['failed']} case(s) failed — quality gate BLOCKS.")
    else:
        L.append("  All cases passed — no regression.")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Offline eval / quality-regression runner.")
    ap.add_argument("--dataset", required=True,
                    help="dataset dir OR a bare name under eval/datasets/<name>")
    ap.add_argument("--offline", default=None,
                    help="grade pre-captured {case_id,output} from this .jsonl instead of live replay")
    ap.add_argument("--router-url", default=DEFAULT_ROUTER_URL,
                    help=f"router base URL (default {DEFAULT_ROUTER_URL}; env ROUTER_URL)")
    ap.add_argument("--router-token", default=DEFAULT_ROUTER_TOKEN,
                    help="router token (default env ROUTER_TOKEN)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    ap.add_argument("--record", action="store_true",
                    help="append this run to the results ledger (eval/results.jsonl)")
    ap.add_argument("--label", default=None,
                    help="name for this run/variant, recorded in the ledger")
    ap.add_argument("--override", default=None,
                    help='JSON of request overrides merged into each replay, e.g. \'{"tier":"t3"}\'')
    ap.add_argument("--resample", type=int, default=1, metavar="N",
                    help="run each case N times through the router and report AGENT "
                         "stability (live replay only; --judge-votes measures graders)")
    ap.add_argument("--judge-votes", type=int, default=1, metavar="N",
                    help="run N independent judges per judge-graded case; confidence "
                         "is their agreement. Use an odd N — an even split fails.")
    args = ap.parse_args(argv)

    overrides = None
    if args.override:
        try:
            overrides = json.loads(args.override)
            if not isinstance(overrides, dict):
                raise ValueError("must be a JSON object")
        except (ValueError, json.JSONDecodeError) as e:
            print(f"eval_run: bad --override: {e}", file=sys.stderr)
            return 2
    if args.judge_votes < 1:
        print("eval_run: --judge-votes must be >= 1", file=sys.stderr)
        return 2
    if args.judge_votes > 1 and args.judge_votes % 2 == 0:
        # Not fatal, but say it: an even panel makes ties possible, and this
        # resolves them to FAIL. Better to be told than to discover it as a
        # mysterious regression.
        print(f"eval_run: WARNING --judge-votes={args.judge_votes} is even; "
              "a tie resolves to FAIL. Prefer an odd number.", file=sys.stderr)

    try:
        cases = load_cases(args.dataset)
    except ValueError as e:
        print(f"eval_run: {e}", file=sys.stderr)
        return 2

    offline = None
    if args.offline:
        try:
            offline = load_offline(args.offline)
        except (ValueError, OSError, json.JSONDecodeError) as e:
            print(f"eval_run: bad --offline file: {e}", file=sys.stderr)
            return 2

    if args.resample > 1 and args.offline:
        print("eval_run: --resample is ignored with --offline (grading one captured "
              "output N times measures nothing).", file=sys.stderr)
    res = run_eval(cases, offline=offline,
                   router_url=args.router_url, token=args.router_token,
                   overrides=overrides, votes=args.judge_votes, resample=args.resample)
    print(json.dumps(res, indent=2) if args.format == "json" else render_text(res))
    if args.record:
        rec = record_result(res, args.dataset, label=args.label, overrides=overrides)
        print(f"\nrecorded -> {RESULTS_PATH}  ({rec['ts']}, label={rec['label']})")
    # CI quality gate: non-zero on ANY regression.
    return 1 if res["regression"] else 0


if __name__ == "__main__":
    sys.exit(main())
