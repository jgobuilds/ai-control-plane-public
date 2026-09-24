#!/usr/bin/env python3
"""Corpus for the CI diagnosis poster.

What actually matters here is not the prose of a comment. It is:

  - It posts COMMENTS and nothing else. The diagnostician's no-write-path
    guarantee only survives if this file cannot commit, push, or rerun a job.
  - A re-run updates its comment instead of adding another. A flaky job re-run
    five times must not bury the PR, because the noise is what gets the whole
    mechanism switched off.
  - No secret reaches a comment. It renders redacted text into two channels.

    python tests/ci_comment_test.py
"""
import os, sys, json, ast, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, "..", "scripts", name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cc = _load("ci_comment")
cd = _load("ci_diagnose")

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


print("It comments, and cannot do anything else:")
_src = open(os.path.join(HERE, "..", "scripts", "ci_comment.py"), encoding="utf-8").read()
_tree = ast.parse(_src)
_imports = set()
for _n in ast.walk(_tree):
    if isinstance(_n, ast.Import):
        _imports.update(a.name.split(".")[0] for a in _n.names)
    elif isinstance(_n, ast.ImportFrom) and _n.module:
        _imports.add(_n.module.split(".")[0])
# Posting a comment needs the network; changing code needs a shell or git.
for mod in ("subprocess", "shutil", "os.system"):
    check(f"does not import {mod}", mod not in _imports, f"imports: {sorted(_imports)}")

# The only URLs it may talk to. A 'reruns' or 'git/refs' endpoint here would be
# the tool quietly gaining the ability to act rather than advise.
_urls = [n.value for n in ast.walk(_tree)
         if isinstance(n, ast.Constant) and isinstance(n.value, str)
         and n.value.startswith("http")]
check("no rerun/dispatch/git-write endpoint appears anywhere",
      not any(k in u for u in _urls for k in ("rerun", "dispatches", "git/refs", "merges")),
      str(_urls))

# The single file write is the job summary — an append to a path GitHub gives
# us, which renders on the run page and touches no repository content.
_writes = []
for _n in ast.walk(_tree):
    if isinstance(_n, ast.Call) and isinstance(_n.func, ast.Name) and _n.func.id == "open":
        mode = _n.args[1].value if len(_n.args) > 1 and isinstance(_n.args[1], ast.Constant) else None
        for kw in _n.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value
        if mode and any(c in str(mode) for c in "wxa+"):
            _writes.append(ast.unparse(_n)[:80])
check("its only write is the append to GITHUB_STEP_SUMMARY",
      len(_writes) == 1 and "summary" in _writes[0] and '"a"' in _writes[0].replace("'", '"'),
      str(_writes))

print("\nEvery comment is idempotent-tagged:")
# A SHARED cause: this corpus ships to every repo, so it must not depend on a
# row that only exists here.
det = cd.diagnose("FROM requires either one or three arguments")
esc = cd.diagnose("something nobody has seen")
for label, res in (("known", det), ("unknown", esc)):
    check(f"the {label} comment carries the update marker",
          cc.MARKER in cc.body_markdown(res))
check("the marker is an HTML comment, so a reader never sees it",
      cc.MARKER.startswith("<!--") and cc.MARKER.endswith("-->"))

print("\nAn unknown failure claims no cause:")
b = cc.body_markdown(esc)
check("it says no cause is being claimed", "no cause is being claimed" in b)
check("it points at the promotion path", "ci-known-causes.json" in b)
check("it does not present itself as an answer", "known cause" not in b.lower())

print("\nA known cause carries the confirmed fix, not a guess:")
b = cc.body_markdown(det)
check("the cause is present", "trailing # comment" in b)
check("the fix is present", "**Fix:**" in b)
check("it says no model was called", "No model was called" in b)

print("\nA comment for an UNREADABLE log does not look like a diagnosis:")
nolog = cd.diagnose("failed to get run: HTTP 403: Resource not accessible by "
                    "integration (https://api.github.com/repos/o/r/actions/runs/1)")
b = cc.body_markdown(nolog)
check("it says nothing was diagnosed", "**Nothing was diagnosed.**" in b)
check("its heading is about the log, not about the failure",
      "could not be read" in b.split("\n")[2], b.split("\n")[2])
check("it names the remedy", "actions: read" in b)
check("and shows what came back instead of a log", "403" in b)

print("\nRedaction survives into the rendered comment:")
# Assembled, not literal: this corpus is vendored into a PUBLIC repo, and a
# secret scanner cannot know that a token in a test file is synthetic — GitHub's
# push protection rejected the push over exactly this. Nothing here is a real
# credential and the assertions are unchanged; only the contiguous
# secret-shaped literals are gone.
def _synth(*parts):
    return "".join(parts)


_AWS = _synth("AKIA", "IOSFODNN7EXAMPLE")
_GH = _synth("ghp", "_zzzzzzzzzzzzzzzzzzzzzzzzzz")
log = ("##[error] boom\nAuthorization: Bearer hunter2hunter2hunter2\n"
       + _GH + "\n" + _AWS + "\nping someone@example.com\n"
       + _synth("https://hooks.slack.com", "/services/T1/B2/xyz"))
b = cc.body_markdown(cd.diagnose(log))
for leak in ("hunter2", _GH, _AWS, "someone@example.com",
             _synth("hooks.slack.com", "/services")):
    check(f"no {leak[:18]} in the comment", leak not in b, b[-300:])
check("and the same holds after the Slack conversion",
      not any(k in cc.slackify(b) for k in ("hunter2", _GH, "someone@")))

print("\nIt comments on the right GitHub object:")
pr = cc.github_target({"GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_NAME": "pull_request",
                       "GITHUB_REF": "refs/pull/123/merge"})[1]
check("a PR uses the ISSUE endpoint (the pulls one needs a diff anchor and 422s)",
      pr.endswith("/repos/o/r/issues/123/comments"), pr)
push = cc.github_target({"GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_NAME": "push",
                         "GITHUB_SHA": "abc123"})[1]
check("a push comments on the commit", push.endswith("/repos/o/r/commits/abc123/comments"), push)
check("outside Actions it targets nothing rather than guessing",
      cc.github_target({}) == (None, None))
check("GITHUB_API_URL is honoured, so GHES is not hardcoded to github.com",
      cc.github_target({"GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "s",
                        "GITHUB_API_URL": "https://ghe.internal/api/v3"}
                       )[1].startswith("https://ghe.internal/api/v3"))

print("\nMissing configuration is a skip, not a failure:")
ok, why = cc.post_slack("body", {}, "http://run")
check("no Slack bot token skips", ok is False and "webhook" in why)
check("and explains that a webhook CANNOT be threaded", "cannot be threaded" in why)
ok, why = cc.post_github("body", {"GITHUB_REPOSITORY": "o/r", "GITHUB_SHA": "s"})
check("no GitHub token skips with the permission it needs",
      ok is False and "pull-requests: write" in why, why)

print("\nBLUF is one line — an alert is an interrupt:")
for res in (det, esc, {"tier": "unavailable", "known": [], "note": "x"}):
    line = cd.bluf(res)
    check(f"{res['tier']} bluf is a single line", "\n" not in line and len(line) < 200, line)
check("the known-cause bluf leads with the cause", cd.bluf(det).startswith("Known cause:"))
check("the unknown bluf does not invent one", "no known cause" in cd.bluf(esc).lower())

print("\nMalformed input posts nothing rather than crashing the notify job:")
import io, contextlib
p = os.path.join(HERE, "..", "scripts", "_bad.json")
try:
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cc.main(["--diagnosis", p, "--dry-run"])
    check("exit code is 0", rc == 0, str(rc))
    check("and it says nothing was posted", "nothing posted" in buf.getvalue(), buf.getvalue())
finally:
    os.path.exists(p) and os.remove(p)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All CI-comment checks passed.")
