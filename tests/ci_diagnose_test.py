#!/usr/bin/env python3
"""Corpus for the CI diagnostician.

Two properties matter more than the matching itself:

  - It must NEVER claim a cause it does not have. A confident wrong diagnosis
    sends someone down the wrong path, which is worse than "unknown".
  - It must NEVER leak a secret. CI logs are full of them, and this tool's whole
    job is to forward log excerpts to a model and a chat channel.

    python tests/ci_diagnose_test.py
"""
import os, sys, json, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "ci_diagnose", os.path.join(HERE, "..", "scripts", "ci_diagnose.py"))
cd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cd)

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


print("Known causes match deterministically (no model needed):")
# SHARED rows only. This corpus is vendored into every repo alongside the tool,
# so an assertion about a repo-LOCAL row would pass here and fail everywhere
# else — a test that only works at home is worse than no test. (It did: the
# first version of this list asserted three local rows and went green here.)
for log, want in [
    ("BROKEN LINKS: 1\n  a.md  ->  ../../../other-repo/x.md", "cross-repo-link-not-in-ci"),
    # The same cause, two messages, two signatures. The original keyed on
    # "would be reformatted" and so caught only `ruff format`; the next repo in
    # the sweep failed on `ruff check` instead. Widening it to match "ruff"
    # then fired on a CLEAN format run — `match` is AND-only across the whole
    # log and cannot express "either message".
    ("ruff format --check src tests\nWould reformat: scripts\\pii_scan.py",
     "vendored-file-reformatted"),
    ("Running ruff check src tests\nFound 20 errors.", "vendored-file-fails-the-linter"),
    ("FROM requires either one or three arguments", "docker-from-trailing-comment"),
    ("Unable to resolve action `aquasecurity/trivy-action@0.28.0`", "action-version-does-not-exist"),
]:
    res = cd.diagnose(log)
    ids = [k["id"] for k in res["known"]]
    check(f"{want}", res["tier"] == "deterministic" and want in ids, f"got {ids}")

print("\nFalse positives found in the wild are permanent cases:")
# `same_line` exists because a security check prints its rule name whether it
# PASSES or FAILS, so a whole-log signature matched a SUCCESSFUL run. The real
# log text is kept verbatim; the CAUSE it came from is repo-local, so the
# mechanism is exercised against a synthetic table rather than a shipped row.
_sl = [{"id": "same-line-rule", "same_line": True,
        "match": ["FAIL", "uses required ${VAR:?} form"], "cause": "c", "fix": "f"}]
# The shape that actually produced it: one check FAILS, an unrelated check
# PASSES, and the passing line is the one carrying the rule name. Whole-log
# matching sees "FAIL" from the first and the rule text from the second and
# joins them into a cause that is not there.
_passing = ("  FAIL  some entirely different check\n"
            "  PASS  n8n: ROUTER_TOKEN uses required ${VAR:?} form\n"
            "  All security-posture checks passed.")
check("a PASSING check is not diagnosed as a failure",
      cd.match_known(_passing, _sl) == [], str(cd.match_known(_passing, _sl)))
_failing = "FAILED: 1 check(s): n8n: ROUTER_TOKEN uses required ${VAR:?} form"
check("but the real FAILURE line still matches",
      [c["id"] for c in cd.match_known(_failing, _sl)] == ["same-line-rule"])
# Without the flag the SAME table matches that passing run — the entire reason
# the flag exists. Asserting it stops someone "simplifying" it away later.
check("without same_line, that passing run WOULD have matched",
      cd.match_known(_passing, [{**_sl[0], "same_line": False}]) != [])

print("\nA CLEAN run of the same tool must diagnose nothing:")
# The failure that produced these two rows also produced a false positive on the
# way: a signature loosened to "ruff" fired on a successful format check. A
# known-cause table earns its keep by being right about the negative cases.
for lbl, log in [("clean ruff check", "Running ruff check src tests\nAll checks passed!"),
                 ("clean ruff format", "ruff format --check\n67 files already formatted")]:
    hits = [k["id"] for k in cd.diagnose(log + "\n" + "x" * 2100)["known"]]
    check(f"{lbl} claims no cause", hits == [], str(hits))

print("\nAnd THIS suite's own output is not a linter failure:")
# Third time for the same collision, second time for this row. The signature was
# `ruff` AND `Found ` anywhere in the whole log. The lines above supply `ruff`
# ("clean ruff check claims no cause") and a heading forty lines up supplies
# `Found ` ("False positives found in the wild") — so the cause table matched the
# test that tests the cause table, and two ai-control-plane runs were signed with
# a cause that was not theirs. The row is now `same_line` on ruff's summary line.
_self = open(os.path.join(HERE, "fixtures", "ci-log-self-referential-ruff.txt"),
             encoding="utf-8").read()
_LINT = "vendored-file-fails-the-linter"
_ids = [k["id"] for k in cd.diagnose(_self)["known"]]
check("our own PASS lines do not claim the linter row", _LINT not in _ids, str(_ids))
check("and nothing else is claimed off them either", _ids == [], str(_ids))
# The mechanism, pinned. Without the flag the shipped row matches that log again,
# which is the entire reason the flag is on it.
_row = next(c for c in cd.load_causes() if c["id"] == _LINT)
check("without same_line, this suite's output WOULD have matched",
      cd.match_known(_self, [{**_row, "same_line": False,
                              "match": ["ruff", "Found "],
                              "not_match": ["All checks passed"]}]) != [])
# ...and it must still fire on the failure it was written for. Both ruff plurals,
# because "Found 1 error." and "Found 20 errors." are the same signature.
for n, plural in ((1, "error"), (20, "errors")):
    hits = [k["id"] for k in cd.diagnose(
        f"Running ruff check src tests\n"
        f"scripts/pii_scan.py:1:8: F401 [*] `os` imported but unused\n"
        f"Found {n} {plural}.")["known"]]
    check(f"a real ruff check failure ({n} {plural}) still matches",
          hits == [_LINT], str(hits))
# mypy runs in the SAME job as ruff in two consuming repos and phrases its
# summary the same way. Its failure is a type error, not a vendored file the
# linter rejects, and a confident wrong cause costs a debugging session.
_mypy = ("engine/x.py:12: error: Incompatible return value type\n"
         "Found 3 errors in 1 file (checked 12 source files)")
check("mypy's summary is not diagnosed as the vendored-linter cause",
      _LINT not in [k["id"] for k in cd.diagnose(_mypy)["known"]],
      str([k["id"] for k in cd.diagnose(_mypy)["known"]]))
# The sibling row keys on a different message and must be untouched by all of
# this — one cause, two signatures, and narrowing one must not narrow the other.
check("the `Would reformat` sibling still fires",
      "vendored-file-reformatted" in [k["id"] for k in cd.diagnose(
          "ruff format --check src tests\nWould reformat: scripts\\pii_scan.py"
      )["known"]])

print("\nA near-miss must NOT claim a match:")
res = cd.diagnose("engine: built-in only (gitleaks not installed)")
check("'gitleaks not installed' is the normal case, not the stale-bridge bug",
      res["tier"] == "escalate" or "gitleaks-bridge-stale-cli" not in [k["id"] for k in res["known"]],
      str([k["id"] for k in res["known"]]))
res = cd.diagnose("engine: built-in only")
check("but 'built-in only' WITHOUT that phrase does match the stale bridge",
      "gitleaks-bridge-stale-cli" in [k["id"] for k in res["known"]])

print("\nThe excerpt ranks real failures above our own PASS output:")
# Against a REAL CI log (run 30218852842), the first version returned a wall of
# passing assertions and pushed the actual error off the end — because this
# repo's suites print lines like "PASS  a PASSING security check is not
# diagnosed as a failure". Our vocabulary for describing failures collides with
# the vocabulary of failures. The trimmed real log is the fixture.
_fix = os.path.join(HERE, "fixtures", "ci-log-passing-noise.txt")
_lines = cd.failing_lines(open(_fix, encoding="utf-8").read())
check("the GitHub error marker is surfaced",
      any("##[error]" in l for l in _lines), str(_lines[:3]))
check("so is the line that actually failed",
      any("PROOF-OF-PLUMBING" in l for l in _lines))
check("and NO passing assertion is presented as failing output",
      not any("PASS" in l for l in _lines),
      str([l for l in _lines if "PASS" in l][:3]))
check("section headings are not mistaken for failures",
      not any(l.endswith(":") and "##[error]" not in l for l in _lines),
      str([l for l in _lines if l.endswith(":")][:3]))
check("the excerpt stays short enough to read", len(_lines) <= 5, str(len(_lines)))
# A strong signal must never be evicted by a flood of weak ones.
_flood = "\n".join(["error in step %d" % i for i in range(200)]) + "\n##[error]the real one"
check("a strong line survives 200 weak ones",
      any("the real one" in l for l in cd.failing_lines(_flood)))

print("\nDiagnosis is a root-cause review, not a symptom report:")
_esc = cd.diagnose("something nobody has seen")["escalation"]
for section in ("PROXIMATE CAUSE", "ROOT CAUSE", "WHY IT WAS NOT CAUGHT SOONER",
                "FIX", "PREVENTION"):
    check(f"the prompt asks for {section}", section in _esc["prompt"])
check("prevention must be a MECHANISM, not an intention",
      "not an intention" in _esc["prompt"] and "Be more careful" in _esc["prompt"])
check("it bounds the why-chain to the evidence",
      "stop when you leave the evidence" in _esc["prompt"])
# Every shipped row must carry the mechanism, not just the fix. `fix` addresses
# this run; `prevent` is what stops the third occurrence, and it is the half
# that gets skipped once the build is green again.
_all = cd.load_causes()
_no_mech = [c["id"] for c in _all if not c.get("prevent")]
check("every known cause records a prevention mechanism", _no_mech == [], str(_no_mech))
check("fix and prevent are different text",
      all(c.get("prevent") != c.get("fix") for c in _all))

print("\n'Has this workflow EVER passed?' changes the answer:")
# A first-run failure and a regression look identical in a run list and need
# opposite responses. One repo here failed 7 for 7 while everyone looked for
# what the last commit had broken; the gate was the thing that was wrong.
_never = cd.diagnose("something nobody has seen", ever_green=False)
check("the escalation tells the model to suspect the GATE",
      "Treat the GATE as the primary suspect" in _never["escalation"]["prompt"])
check("and to rule that out before blaming the commit",
      "Do NOT recommend changes to the commit" in _never["escalation"]["prompt"])
check("the rendered report leads with it",
      cd.render(_never).splitlines()[0].startswith("!! This workflow has NEVER passed"))
# It applies even when a cause DID match: a matched signature explains the
# symptom, not why no commit has ever satisfied the gate.
_known_never = cd.diagnose("FROM requires either one or three arguments", ever_green=False)
check("a matched cause does not suppress the never-green warning",
      "NEVER passed" in _known_never["note"], _known_never["note"])
check("unknown history stays silent rather than guessing",
      "NEVER" not in cd.render(cd.diagnose("something nobody has seen")),
      cd.render(cd.diagnose("something nobody has seen"))[:80])
check("a workflow that HAS passed gets no warning",
      "NEVER" not in cd.render(cd.diagnose("something nobody has seen", ever_green=True)))

print("\nAn UNREADABLE log is not an unrecognised failure:")
# Found on the first real run (PR #3). A restrictive permissions: block dropped
# actions: read, `gh run view` returned 403, and the tool faithfully diagnosed
# the error message as an unrecognised failure. Every failure would have been
# reported that way indefinitely, and the output looked entirely plausible.
_403 = ("failed to get run: HTTP 403: Resource not accessible by integration "
        "(https://api.github.com/repos/o/r/actions/runs/1)")
res = cd.diagnose(_403)
check("a 403 log-fetch is tier no-log", res["tier"] == "no-log", res["tier"])
# The HEADLINE is what a skimming reader takes away, so that is what is
# asserted — the body says "this is not an unrecognised failure" and a naive
# substring search over the whole render flags its own disclaimer.
check("the headline says the log was unavailable, not that the failure is unknown",
      cd.render(res).splitlines()[0].strip() == "CI diagnosis — LOG UNAVAILABLE",
      cd.render(res).splitlines()[0])
check("it names the actual remedy", "actions: read" in res["note"])
check("an EMPTY log is also no-log, not a clean bill of health",
      cd.diagnose("   \n  ")["tier"] == "no-log")
check("the bluf says nothing was diagnosed",
      "nothing diagnosed" in cd.bluf(res).lower(), cd.bluf(res))
# The guard is bounded so it cannot swallow a real failure that merely mentions
# a 40x somewhere: a full-length log is diagnosed normally regardless.
_real = _403 + "\n" + ("x" * 2100) + "\nis not writable"
check("a real, full-length log is still diagnosed normally",
      cd.diagnose(_real)["tier"] == "deterministic", cd.diagnose(_real)["tier"])

print("\nAn unknown failure escalates rather than inventing a cause:")
res = cd.diagnose("Traceback (most recent call last):\n  ZeroDivisionError: division by zero")
check("tier is escalate", res["tier"] == "escalate", res["tier"])
check("no cause is claimed", res["known"] == [])
check("it carries a prompt for the model tier", "escalation" in res)
check("the escalation is advise-only",
      res["escalation"]["action"] == "advise", res["escalation"].get("action"))
check("it tells the reader to promote a confirmed fix",
      "ci-known-causes.json" in res["note"])

print("\nRedaction — the tool forwards log text, so this is load-bearing:")
# The fixtures are ASSEMBLED, not written as literals. This corpus is vendored
# into every repo including a PUBLIC one, and GitHub's push protection rejected
# the push over a Slack-token-shaped literal here — correctly: a scanner cannot
# know a token in a test file is synthetic, and a scanner that guesses is
# useless. Our own gate had allowed it via a marker-anchored .pii-allow entry,
# which was narrow but still an exception; assembling removes the need for the
# exception entirely, in this repo and every other.
#
# This is NOT obfuscation to sneak something past a scanner. Nothing here is a
# real credential, redact() still receives the fully-formed string, and the test
# is exactly as strong as before — the only change is that no contiguous
# secret-shaped literal exists in any file we ship.
def _synth(*parts):
    return "".join(parts)


secrets = [
    (_synth("xoxb", "-1234567890-", "abcdefghijklmno"), "«slack-token»"),
    (_synth("ghp", "_abcdefghijklmnopqrstuvwxyz0123"), "«github-token»"),
    (_synth("AKIA", "IOSFODNN7EXAMPLE"), "«aws-key»"),
    (_synth("https://hooks.slack.com", "/services/T1/B2/xyzabc"), "«slack-webhook»"),
    ("Authorization: Bearer supersecretvalue", "«redacted»"),
    (_synth("-----BEGIN ", "RSA PRIVATE KEY", "-----"), "«private-key»"),
    ("someone@example.com", "«email»"),
]
for raw, marker in secrets:
    out = cd.redact(f"error at line 3: {raw} boom")
    check(f"redacts {marker}", raw not in out and marker in out, out[:70])

print("\nRedaction reaches the ESCALATION PAYLOAD, not just the helper:")
# Assembled for the same reason as the fixtures above.
log = ("##[error] failed\nAuthorization: Bearer hunter2hunter2hunter2\n"
       + _synth("ghp", "_zzzzzzzzzzzzzzzzzzzzzzzzzz"))
esc = cd.diagnose(log)["escalation"]
check("no bearer value in the prompt", "hunter2" not in esc["prompt"], esc["prompt"][-160:])
check("no github token in the prompt", "ghp_zzzz" not in esc["prompt"])

print("\nIt asks the model to flag check-weakening fixes:")
esc = cd.diagnose("something unrecognised")["escalation"]
check("the prompt demands a check-weakening fix be called out first",
      "WEAKEN A CHECK" in esc["prompt"] and "first and loudly" in esc["prompt"],
      esc["prompt"][-400:])
check("and invites 'I cannot tell' over a guess",
      "cannot tell" in esc["prompt"].lower())

print("\nTwo tables, unioned — the split that lets this file be vendored:")
_real_c, _real_l = cd.CAUSES, cd.LOCAL_CAUSES
_tmp = os.path.join(HERE, "_tbl")
try:
    os.makedirs(_tmp, exist_ok=True)
    shared_p, local_p = os.path.join(_tmp, "s.json"), os.path.join(_tmp, "l.json")
    with open(shared_p, "w", encoding="utf-8") as fh:
        json.dump({"causes": [{"id": "shared-one", "match": ["alpha"], "cause": "c",
                               "fix": "f"},
                              {"id": "both", "match": ["beta"], "cause": "SHARED",
                               "fix": "f"}]}, fh)
    with open(local_p, "w", encoding="utf-8") as fh:
        json.dump({"causes": [{"id": "local-one", "match": ["gamma"], "cause": "c",
                               "fix": "f"},
                              {"id": "both", "match": ["beta"], "cause": "LOCAL",
                               "fix": "f"}]}, fh)
    cd.CAUSES, cd.LOCAL_CAUSES = shared_p, local_p
    check("a shared cause matches", cd.diagnose("alpha")["known"][0]["id"] == "shared-one")
    check("a repo-local cause matches", cd.diagnose("gamma")["known"][0]["id"] == "local-one")
    # A repo that has re-diagnosed a shared cause in its own terms knows
    # something the shared table does not — and it must not get BOTH answers.
    hits = cd.diagnose("beta")["known"]
    check("a duplicate id resolves to the LOCAL row, once",
          len(hits) == 1 and hits[0]["cause"] == "LOCAL", str(hits))

    cd.LOCAL_CAUSES = os.path.join(_tmp, "absent.json")
    check("NO local table is normal, not an error",
          cd.diagnose("alpha")["tier"] == "deterministic")
    # But a missing SHARED table means a broken vendoring, and must not read the
    # same as "this repo happens to have no local rows".
    cd.CAUSES = os.path.join(_tmp, "absent.json")
    check("a missing SHARED table is unavailable, not empty",
          cd.diagnose("alpha")["tier"] == "unavailable")
finally:
    cd.CAUSES, cd.LOCAL_CAUSES = _real_c, _real_l
    import shutil as _sh
    _sh.rmtree(_tmp, ignore_errors=True)

print("\nA broken table is not 'no known causes':")
real = cd.CAUSES
try:
    cd.CAUSES = os.path.join(HERE, "does-not-exist.json")
    res = cd.diagnose("anything")
    check("tier is unavailable, not escalate", res["tier"] == "unavailable", res["tier"])
    check("and it does not escalate on a broken table", "escalation" not in res)
finally:
    cd.CAUSES = real

print("\nIt has no write path at all:")
import ast as _ast

_src = open(os.path.join(HERE, "..", "scripts", "ci_diagnose.py"), encoding="utf-8").read()
_tree = _ast.parse(_src)

# Check the CODE, not the prose. The tool's docstring explains at length why it
# must never commit or push, and a substring search over the raw file flags its
# own explanation — the same false positive a `set -e` check produced earlier in
# this project. Comments and docstrings are stripped by parsing.
_imports = set()
for _n in _ast.walk(_tree):
    if isinstance(_n, _ast.Import):
        _imports.update(a.name.split(".")[0] for a in _n.names)
    elif isinstance(_n, _ast.ImportFrom) and _n.module:
        _imports.add(_n.module.split(".")[0])

for mod in ("subprocess", "shutil", "requests", "urllib"):
    check(f"does not import {mod}", mod not in _imports, f"imports: {sorted(_imports)}")

# The only file access it may perform is READING a log and the causes table.
_writes = []
for _n in _ast.walk(_tree):
    if isinstance(_n, _ast.Call) and isinstance(_n.func, _ast.Name) and _n.func.id == "open":
        mode = None
        for kw in _n.keywords:
            if kw.arg == "mode" and isinstance(kw.value, _ast.Constant):
                mode = kw.value.value
        if len(_n.args) > 1 and isinstance(_n.args[1], _ast.Constant):
            mode = _n.args[1].value
        if mode and any(c in str(mode) for c in "wxa+"):
            _writes.append(_ast.unparse(_n)[:60])
check("every open() is read-only", _writes == [], str(_writes))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All CI-diagnostician checks passed.")
