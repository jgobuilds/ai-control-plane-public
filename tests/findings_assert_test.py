#!/usr/bin/env python3
"""Run every finding's proof. "Closed" must be a command that passes, not a word.

The cross-check gate proves the documents AGREE about a finding's status. It
cannot prove they are RIGHT — a finding marked fixed everywhere and actually
broken sails through it. This closes that gap: `security/findings.yaml` makes
every `closed` and `partial` finding name a command, and this runs them.

WHY A SUBSTRING AND NOT JUST AN EXIT CODE. A suite that still exits zero after
someone deletes the single check that mattered would satisfy "exit 0" and prove
nothing. The expected substring names the specific assertion, so removing it
fails the finding that depends on it — which is the difference between "the
tests pass" and "this finding is fixed".

WHAT IT STILL CANNOT DO. It cannot tell you the assertion is a GOOD one. A
finding whose proof is a check that passes vacuously is still wrong, just wrong
with a receipt. That is why the substring must name a real assertion and why
several of these were written by breaking them first.

    python tests/findings_assert_test.py          # run every proof
    python tests/findings_assert_test.py --list   # show the ledger, run nothing
"""
import os, subprocess, sys

try:
    import yaml
except ImportError:
    print("  pyyaml is required (pip install pyyaml)")
    sys.exit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER = os.path.join(ROOT, "security", "findings.yaml")
TM = os.path.join(ROOT, "docs", "security", "THREAT-MODEL.md")

NEEDS_PROOF = {"closed", "partial"}
VALID = {"open", "closed", "partial", "accepted"}

FAILS = []
UNVERIFIED = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


with open(LEDGER, encoding="utf-8") as fh:
    data = yaml.safe_load(fh)
findings = data["findings"]

if "--list" in sys.argv:
    for f in findings:
        proof = f.get("asserted_by", {}).get("cmd", "—")
        print(f"  {f['id']:4s} {f['status']:9s} {f['title'][:48]:50s} {proof}")
    sys.exit(0)

print("The ledger is well-formed:")
ids = [f["id"] for f in findings]
check("no duplicate ids", len(ids) == len(set(ids)),
      str([i for i in ids if ids.count(i) > 1]))
bad = [f["id"] for f in findings if f["status"] not in VALID]
check("every status is a known value", not bad, str(bad))

print("\nEvery finding in the threat model is in the ledger, and vice versa:")
# A finding that exists in one and not the other is how a status goes
# unmaintained — which is the failure this whole pass is about.
with open(TM, encoding="utf-8") as fh:
    tm_text = fh.read()
missing_from_tm = [i for i in ids if i not in tm_text]
check("ledger ids all appear in THREAT-MODEL.md", not missing_from_tm, str(missing_from_tm))

print("\nEvery CLOSED or PARTIAL finding names a proof:")
for f in findings:
    if f["status"] not in NEEDS_PROOF:
        continue
    ab = f.get("asserted_by")
    check(f"{f['id']} ({f['status']}) declares asserted_by",
          bool(ab and ab.get("cmd") and ab.get("expect")),
          "a closed finding without a proof is an assertion, not a fact")

print("\nEvery ACCEPTED finding says why (that is the whole point of the label):")
for f in findings:
    if f["status"] != "accepted":
        continue
    check(f"{f['id']} explains why it is accepted", bool(f.get("why")),
          "accepted without a reason is indistinguishable from forgotten")

print("\nEvery PARTIAL finding names what is still open:")
for f in findings:
    if f["status"] != "partial":
        continue
    check(f"{f['id']} declares its residual", bool(f.get("residual")),
          "half-fixed with no stated residual reads as fixed")

def tool_available(name):
    from shutil import which
    return which(name) is not None


IN_CI = bool(os.environ.get("CI"))

print("\nRunning each proof — this is the part that makes 'closed' falsifiable:")
if not IN_CI:
    print("  (local: a proof needing a tool this machine lacks reports UNVERIFIED,\n"
          "   never PASS. CI has every tool and treats the same case as a failure.)")
cache = {}
for f in findings:
    ab = f.get("asserted_by")
    if not ab or not ab.get("cmd"):
        continue
    cmd, expect = ab["cmd"], ab["expect"]
    need = ab.get("requires")
    # An explicit seam, not a silent skip. Some proofs need a runtime this
    # machine does not have — node lives only in the containers here, while CI
    # has it. Reporting that as PASS would be a loophole; failing every local
    # run trains people to ignore the gate. So: UNVERIFIED locally, hard failure
    # in CI, and the distinction is printed rather than assumed.
    if need and not tool_available(need) and not IN_CI:
        print(f"  UNVERIFIED  {f['id']}: needs {need!r}, absent here — CI verifies this")
        UNVERIFIED.append(f["id"])
        continue
    if cmd not in cache:
        r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        cache[cmd] = (r.returncode, (r.stdout or "") + (r.stderr or ""))
    rc, out = cache[cmd]
    ok = rc == 0 and expect in out
    # "Could not run" and "ran and failed" are different diagnoses, and only one
    # of them means the control is broken. Both still FAIL: a claim that cannot
    # be checked here is not a verified claim, and silently passing an
    # unrunnable proof is how a ledger becomes decoration.
    unrunnable = ("not found" in out.lower() or "not recognized" in out.lower()
                  or "no such file" in out.lower())
    if rc != 0 and unrunnable:
        detail = f"COMMAND COULD NOT RUN here — {out.strip().splitlines()[-1][:80] if out.strip() else 'no output'}"
    elif rc != 0:
        detail = "command exited %d — the control is failing, not missing" % rc
    else:
        detail = f"ran, but output never contained {expect!r}"
    check(f"{f['id']}: {cmd}", ok, detail)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for x in FAILS:
        print("  - " + x)
    print("\n  A finding whose proof does not run is not closed. Either fix the\n"
          "  assertion, or change the status to what is actually true.")
    sys.exit(1)
# Do not claim what was not checked. Saying "all verified" while one proof was
# skipped is the same overclaim this ledger exists to stop, made by the ledger.
if UNVERIFIED:
    print(f"{len(findings) - len(UNVERIFIED)} of {len(findings)} finding(s) verified here; "
          f"{len(UNVERIFIED)} UNVERIFIED on this machine: {', '.join(UNVERIFIED)}.")
    print("  Not a pass for those — CI runs them.")
else:
    print(f"All {len(findings)} finding(s) verified — every closure runs.")
