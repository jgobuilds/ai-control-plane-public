#!/usr/bin/env python3
"""Offline eval runner — the QUALITY-regression counterpart to the eval/drift lane.

Where scripts/eval_metrics.py measures *metadata the router already logged* (a
proxy — see EVAL.md), THIS runs a real semantic/ground-truth eval: it takes a set
of regression CASES (captured from production failures + curated examples), REPLAYS
each through the router, and GRADES the output against an expected value / pattern /
goal. It is the offline quality gate that closes the semantic-eval gap EVAL.md flags.

Adapted from LangChain's "operationalize the agent improvement loop" (production
trace -> capture -> offline eval against a regression dataset -> fix -> prevent
recurrence), done the Brightworks way: governed observability — eval data stays
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

ALLOWED_GRADERS = ("exact", "contains", "regex", "judge")

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


def grade(output, grader, judge_fn=None):
    """Dispatch to the right grader. Returns (passed, detail)."""
    gtype = (grader or {}).get("type")
    if gtype == "exact":
        return grade_exact(output, grader)
    if gtype == "contains":
        return grade_contains(output, grader)
    if gtype == "regex":
        return grade_regex(output, grader)
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


def replay(case, router_url, token):
    """Replay one case through POST /route. Returns (output_text, error_or_None)."""
    payload = {
        "prompt": case["prompt"],
        "scope": case["scope"],
        "task_type": case.get("task_type"),
        "action": case.get("action", "advise"),
        "trigger": "turn",
    }
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
def run_eval(cases, offline=None, router_url=None, token=None):
    """Grade every case. offline: {case_id: output} to grade instead of replaying.
    Returns a result dict with per-case rows + a summary."""
    rows = []
    for case in cases:
        cid = case["id"]
        grader = case["grader"]
        judge_fn = None
        error = None
        if offline is not None:
            if cid not in offline:
                output, error = "", "no offline output for this case"
            else:
                output = offline[cid]
            if grader.get("type") == "judge" and router_url:
                judge_fn = make_router_judge(router_url, token, case["scope"])
        else:
            output, error = replay(case, router_url, token)
            if grader.get("type") == "judge":
                judge_fn = make_router_judge(router_url, token, case["scope"])
        if error:
            rows.append({"id": cid, "passed": False, "detail": error,
                         "grader": grader.get("type"), "scope": case["scope"]})
            continue
        passed, detail = grade(output, grader, judge_fn=judge_fn)
        rows.append({"id": cid, "passed": bool(passed), "detail": detail,
                     "grader": grader.get("type"), "scope": case["scope"]})
    passed = sum(1 for r in rows if r["passed"])
    failed = len(rows) - passed
    return {
        "total": len(rows),
        "passed": passed,
        "failed": failed,
        "regression": failed > 0,
        "mode": "offline" if offline is not None else "live-replay",
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
    args = ap.parse_args(argv)

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

    res = run_eval(cases, offline=offline,
                   router_url=args.router_url, token=args.router_token)
    print(json.dumps(res, indent=2) if args.format == "json" else render_text(res))
    # CI quality gate: non-zero on ANY regression.
    return 1 if res["regression"] else 0


if __name__ == "__main__":
    sys.exit(main())
