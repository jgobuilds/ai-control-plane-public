#!/usr/bin/env python3
"""Diagnose a failed CI run. ADVISE ONLY — it never changes anything.

WHY IT CANNOT FIX. The obvious version of this tool commits a fix and pushes.
Look at what CI actually failed on recently: a gate that was too strict, a check
whose regex was wrong, a token form. The cheapest way to make CI green is almost
always to WEAKEN A CHECK — so an agent rewarded for green output learns to file
down the safety net, and it does it at 3am with nobody reading. This tool
therefore has no write path at all, not merely a disabled one.

THE LOOP — deterministic work before expensive work, applied to CI:

    deterministic  ->  model  ->  human
    known cause        hypothesis   decide

  1. DETERMINISTIC. The known-cause tables are matched first. A hit costs
     nothing, cannot hallucinate, and is the same answer every time. This is
     where confirmed fixes get PROMOTED to: diagnosing a new failure ends with
     adding a row, so the second occurrence is free.

  2. MODEL, only for what the tables miss. The log is REDACTED first (below).
     This step BUILDS a payload; it does not send one. Where a control plane
     exists the payload goes through it — tiered, budgeted, and written to an
     audit ledger — rather than straight to a provider.

  3. HUMAN. The model proposes; a person decides. With ask_human wired, an
     uncertain diagnosis can ask rather than guess.

    python scripts/ci_diagnose.py --log ci.log
    python scripts/ci_diagnose.py --log ci.log --json
"""
from __future__ import annotations
import os, re, sys, json, argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Two tables, unioned. The split exists because this file is VENDORED
# byte-identical across every repo: a single table would either drift (defeating
# the vendoring) or have nowhere to record a cause that is specific to one
# project — and the promotion loop is the whole point of the tool.
#
#   scripts/ci-known-causes.json   shared, vendored, byte-identical. Canonical
#                                  copy in ai-standards. Any repo could hit these.
#   .ci-known-causes.json          this repo's own. Hand-edited, never vendored.
CAUSES = os.path.join(ROOT, "scripts", "ci-known-causes.json")
LOCAL_CAUSES = os.path.join(ROOT, ".ci-known-causes.json")

# Redaction before ANY log leaves this process. A CI log is one of the easiest
# places to leak a token: echoed env, a curl line, a stack trace with a URL. The
# model tier cannot see what it does not need, and neither can a Slack message.
REDACTIONS = [
    (re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})"), "«slack-token»"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "«github-token»"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "«api-key»"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "«aws-key»"),
    (re.compile(r"(?i)(authorization|x-[a-z-]*token|bearer)\s*[:=]\s*\S+"), r"\1: «redacted»"),
    (re.compile(r"https://hooks\.slack\.com/\S+"), "«slack-webhook»"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "«email»"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "«private-key»"),
]


def redact(text):
    for pat, sub in REDACTIONS:
        text = pat.sub(sub, text)
    return text


def _read_table(path, required):
    """Rows from one table. None means "could not read"; [] means "absent"."""
    if not os.path.exists(path):
        # A repo with no local table is the normal case, not a problem. A
        # MISSING shared table is a broken install and must not look the same.
        if required:
            print(f"warning: {path} is missing; the deterministic tier is "
                  f"UNAVAILABLE, not empty", file=sys.stderr)
            return None
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("causes", [])
    except (OSError, ValueError) as e:
        # An unreadable table is not "no known causes" — say which happened, or
        # every failure silently escalates to the model tier and costs money.
        print(f"warning: could not read {path} ({type(e).__name__}); "
              f"the deterministic tier is UNAVAILABLE, not empty", file=sys.stderr)
        return None


def load_causes(path=None, local=None):
    # Resolved at CALL time, not bound as a default at import time — a default
    # argument freezes the value, so the table location could not be overridden
    # for a test or a different deployment. Found by a test that tried.
    shared = _read_table(path or CAUSES, required=True)
    if shared is None:
        return None
    extra = _read_table(local or LOCAL_CAUSES, required=False)
    if extra is None:
        return None
    # Local wins on a duplicate id: a repo that has re-diagnosed a shared cause
    # in its own terms knows something the shared table does not.
    ids = {c.get("id") for c in extra}
    return extra + [c for c in shared if c.get("id") not in ids]


def match_known(log, causes):
    """Every cause whose signature matches. Returns [] for genuinely unknown.

    `same_line` exists because of a real false positive: a signature for a
    FAILING check also matched the log line where that same check PASSED
    ("PASS  n8n: ROUTER_TOKEN uses required ${VAR:?} form"). Whole-log substring
    matching cannot tell a pass from a failure when both mention the rule — so a
    signature that needs it demands all its terms on ONE line.

    Whole-log stays the default, because some real signatures legitimately span
    lines (a header on one, the offending path on another).
    """
    low = log.lower()
    lines = [l.lower() for l in log.splitlines()]
    hits = []
    for c in causes:
        terms = [m.lower() for m in c.get("match", [])]
        nots = [m.lower() for m in c.get("not_match", [])]
        if c.get("same_line"):
            if not any(all(t in line for t in terms) for line in lines):
                continue
            if any(any(n in line for n in nots)
                   for line in lines if all(t in line for t in terms)):
                continue
        else:
            if not all(t in low for t in terms):
                continue
            if any(n in low for n in nots):
                continue      # a near-miss must not claim the match
        hits.append(c)
    return hits


# Selecting failing lines is a RANKING problem, not a filter. The first version
# kept every line matching /fail|error|.../ and took the last 40 — and on a real
# run the excerpt came back as a wall of PASSING assertions, because this repo's
# test suites print lines like "PASS  a PASSING security check is not diagnosed
# as a failure". Our own vocabulary for describing failures collides with the
# vocabulary of actual failures, and the genuine "##[error]Process completed"
# line was pushed off the end by the tail slice.
#
# Same collision as the `same_line` false positive, in a different place. So:
# score lines, drop the ones that announce a success, and keep the strongest.
STRONG = re.compile(
    r"(##\[error\]|^\s*(FAILED|FAIL:|ERROR\b)|Traceback \(most recent call last\)|"
    r"\w*Error: |AssertionError|Process completed with exit code [1-9]|"
    r"^\s*E\s{3})")
WEAK = re.compile(r"(?i)\b(fail|failed|error|traceback|exit code [1-9]|assert)")
# A line that reports a PASS is evidence of the opposite of a failure, however
# many failure words it contains.
PASSING = re.compile(r"(^\s*(PASS|ok|OK)\b|\bPASS\s{2}|✓|passed\.?$)")


def failing_lines(log, limit=40):
    """The lines a human would actually look at, strongest first."""
    strong, weak = [], []
    for line in log.splitlines():
        s = line.strip()[:300]
        if not s or PASSING.search(s):
            continue
        if STRONG.search(s):
            strong.append(s)
        elif WEAK.search(s) and not s.endswith(":"):
            # A weak line ending in a colon is a section HEADING, not a failure
            # — "Servers — auth fail CLOSED (refuse to boot without a token):"
            # is what a passing suite prints above its passing checks. Strong
            # lines are matched first, so "Traceback (most recent call last):"
            # is unaffected.
            weak.append(s)
    # Strong signals are never dropped for weak ones; the tail of the weak list
    # is preferred because a failure's context tends to be near the end.
    return (strong[-limit:] + weak[-(limit - min(len(strong), limit)):])[:limit]


def build_escalation(log, repo="", workflow="", ever_green=None):
    """The prompt for the model tier. Redacted, bounded, and it says what it is.

    Shaped as an RCPS, not a symptom report. Asking only "what's the cause"
    reliably produces the PROXIMATE cause and stops there — the thing that broke,
    not the thing that let it break. Both a review of this project's own CI
    failures and the failure that prompted the review said the same: the fix is
    usually obvious and the reason it survived seven commits is not.
    """
    excerpt = "\n".join(failing_lines(redact(log)))
    never = (
        "\nIMPORTANT: this workflow has NEVER concluded successfully — not once "
        "in its recorded history. Treat the GATE as the primary suspect, not the "
        "commit. A gate that has never passed may be unsatisfiable (asserting "
        "something no commit can produce), may be diffing a file the build "
        "regenerates, or may have been enabled before the thing it checks "
        "existed. Do NOT recommend changes to the commit until you have ruled "
        "that out.\n" if ever_green is False else "")
    return {
        "task_type": "debug",
        "action": "advise",
        "trigger": "ci-failure",
        "ever_green": ever_green,
        "prompt": (
            "A CI run failed and no known-cause signature matched, so this is an "
            "unrecognised failure.\n\n"
            f"Repo: {repo or '(unspecified)'}   Workflow: {workflow or '(unspecified)'}\n"
            f"{never}\n"
            "FAILING LINES (redacted; secrets and addresses are replaced with "
            "«markers» — do not speculate about their contents):\n"
            f"{excerpt or '(no lines matched the failure heuristics)'}\n\n"
            "Answer as a ROOT-CAUSE REVIEW, not a symptom report. Five things, "
            "briefly:\n"
            "1. PROXIMATE CAUSE — what broke, and the exact line above that "
            "shows it. Quote it. If no line supports your answer, say so instead "
            "of supplying one that sounds right.\n"
            "2. ROOT CAUSE — why that was possible. Ask 'why' past the first "
            "answer, but stop when you leave the evidence: two grounded steps "
            "beat five speculative ones.\n"
            "3. WHY IT WAS NOT CAUGHT SOONER — what should have surfaced this "
            "and did not. A failure nobody was told about is a second, separate "
            "defect from the failure itself.\n"
            "4. FIX — the smallest change that addresses the proximate cause.\n"
            "5. PREVENTION — the MECHANISM that stops recurrence. A mechanism, "
            "not an intention: a gate, a test, a default, an untracked file. "
            "'Be more careful' is not an answer; if you have only an intention, "
            "say that no mechanism is apparent.\n\n"
            "Then, before finishing: does the fix WEAKEN A CHECK? If so say it "
            "first and loudly — a green build bought by loosening a gate is "
            "usually the wrong trade, and that call belongs to a person.\n\n"
            "If the log is insufficient, say what is missing rather than "
            "producing a plausible guess. 'I cannot tell from this' is a useful "
            "answer; a confident wrong cause costs a debugging session. This is "
            "not hypothetical — the last root-cause review in this project "
            "blamed line endings, wrote it up and committed it, because the "
            "story was plausible and the real evidence was one line further "
            "down the log that nobody had read."
        ),
    }


# The log fetch failing does NOT look like a failure — it looks like a short log
# with no known signature, which the tool would faithfully report as
# "unrecognised failure". Found the first time this ran for real: a restrictive
# `permissions:` block dropped `actions: read`, `gh run view` returned 403, and
# the PR comment confidently said the failure was unrecognised. Every failure
# would have been diagnosed that way, indefinitely, and the output looked fine.
# "No result" and "no result available" must never render the same.
NO_LOG = re.compile(
    r"(?i)(resource not accessible by integration|failed to get run|"
    r"HTTP 40[0-9]: .*api\.github\.com|gh: .*not found|must authenticate)")


def diagnose(log, repo="", workflow="", ever_green=None):
    """ever_green: has this workflow EVER concluded successfully?

    Passed in rather than looked up — this module has no network, by design. The
    workflow asks GitHub and hands the answer down. It is the single most
    load-bearing fact about a red build and the run list does not show it: a
    first-run failure and a regression look identical there, and they need
    opposite responses. One means the gate was never satisfiable; the other
    means something that worked stopped working.
    """
    if not log.strip() or (len(log) < 2000 and NO_LOG.search(log)):
        return {"tier": "no-log", "known": [],
                "note": ("the failure log could not be READ, so nothing was "
                         "diagnosed — this is not an unrecognised failure. Check "
                         "that the job grants `actions: read`; a restrictive "
                         "permissions: block replaces the defaults rather than "
                         "adding to them."),
                "detail": redact(log.strip()[:600])}
    causes = load_causes()
    if causes is None:
        return {"tier": "unavailable", "known": [],
                "note": "the known-cause table could not be read; NOT escalating "
                        "on a broken table, because the model would be asked to "
                        "re-derive answers we already have"}
    hits = match_known(log, causes)
    if hits:
        return {
            "tier": "deterministic",
            "ever_green": ever_green,
            "known": [{"id": h["id"], "cause": h["cause"], "fix": h["fix"],
                       # `prevent` is the MECHANISM that stops recurrence, kept
                       # separate from `fix` (what to do about this run) because
                       # they are different work and the second is the one that
                       # gets skipped. Optional: a row with no known mechanism
                       # says so rather than inventing one.
                       "prevent": h.get("prevent"),
                       "confidence": h.get("confidence", "unknown")} for h in hits],
            "note": f"{len(hits)} known cause(s) matched — no model call was made."
                    + (" This workflow has NEVER passed, so treat the gate as the "
                       "suspect even though a cause matched." if ever_green is False else ""),
        }
    return {
        "tier": "escalate",
        "known": [],
        "ever_green": ever_green,
        "escalation": build_escalation(log, repo, workflow, ever_green),
        "note": ("no known signature matched. Escalate to the model tier via the "
                 "router (action=advise), then to a human. If the diagnosis is "
                 "confirmed, ADD IT to .ci-known-causes.json (or the shared "
                 "scripts/ci-known-causes.json) so the next "
                 "occurrence is deterministic."),
    }


def bluf(res):
    """One line, bottom-line-up-front. For the alert itself.

    An alert is an INTERRUPT, and the notification standard caps it at six lines
    with the detail behind a link — so the alert gets the conclusion and nothing
    else. Dumping the full diagnosis into the channel is how a useful alert
    becomes one people scroll past. The detail goes in the thread reply and the
    GitHub comment; see scripts/ci_comment.py.
    """
    if res["tier"] == "deterministic":
        first = res["known"][0]
        more = f" (+{len(res['known']) - 1} more)" if len(res["known"]) > 1 else ""
        return f"Known cause: {first['cause']}{more}"
    if res["tier"] == "escalate":
        return "Unrecognised failure — no known cause matched. Needs a look."
    if res["tier"] == "no-log":
        return "Could not read the failure log — nothing diagnosed (check `actions: read`)."
    return "Diagnosis unavailable — the known-cause table could not be read."


def render(res):
    L = []
    if res.get("ever_green") is False:
        L.append("!! This workflow has NEVER passed. Suspect the gate, not the commit.")
    if res["tier"] == "deterministic":
        L.append(f"CI diagnosis — {len(res['known'])} KNOWN cause(s), no model needed")
        for k in res["known"]:
            L.append(f"  [{k['confidence']}] {k['id']}")
            L.append(f"    cause: {k['cause']}")
            L.append(f"    fix:   {k['fix']}")
            L.append(f"    prevent: {k.get('prevent') or '(no mechanism recorded — find one)'}")
    elif res["tier"] == "no-log":
        L.append("CI diagnosis — LOG UNAVAILABLE")
        L.append("  The log could not be read, so NOTHING was diagnosed. This is")
        L.append("  distinct from an unrecognised failure and must not read as one.")
        if res.get("detail"):
            L.append("  what came back instead: " + res["detail"].splitlines()[0][:200])
    elif res["tier"] == "escalate":
        L.append("CI diagnosis — UNKNOWN failure")
        L.append("  No known signature matched. This needs the model tier, then a human.")
        L.append("  Nothing was changed; this tool only advises.")
    else:
        L.append("CI diagnosis — UNAVAILABLE")
    L.append(f"  {res['note']}")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Diagnose a failed CI run (advise only).")
    ap.add_argument("--log", help="path to the failed run's log; '-' for stdin")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--workflow", default=os.environ.get("GITHUB_WORKFLOW", ""))
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--ever-green", dest="ever_green", default="unknown",
                    choices=["yes", "no", "unknown"],
                    help="has this workflow ever concluded success? Supplied by "
                         "the caller; this tool has no network.")
    ap.add_argument("--bluf", action="store_true",
                    help="one line only — for the alert; detail goes in the comment")
    a = ap.parse_args(argv)

    if not a.log:
        ap.error("--log is required (use - for stdin)")
    log = sys.stdin.read() if a.log == "-" else open(
        a.log, encoding="utf-8", errors="replace").read()

    res = diagnose(log, a.repo, a.workflow,
                   {"yes": True, "no": False, "unknown": None}[a.ever_green])
    print(json.dumps(res, indent=2) if a.as_json
          else bluf(res) if a.bluf else render(res))
    # Always 0: this is a REPORT on a failure that already happened. Exiting
    # non-zero would fail the notify job and hide the diagnosis behind a second
    # red X.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
