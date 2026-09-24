#!/usr/bin/env python3
"""Cross-check finding STATUS across the documents that each claim to know it.

WHY THIS EXISTS. A threat-model finding's status is recorded in at least two
Markdown files and referenced from a third YAML file, and **nothing machine-
checked any of them**. In one week seven status claims drifted — a deferral
recorded as still-correct after it closed, lanes described as never having fired
after they fired, a stale *prohibition* that would have changed an agent's
behaviour, a finding marked fixed while half-open, and two documents recording
the same finding with different answers.

WHAT IT CAN AND CANNOT DO. It is a cross-check, not a ledger. It cannot know
whether "FIXED" is TRUE — only whether the documents agree. A finding marked
fixed everywhere and actually broken passes here, and that limit is the argument
for `asserted_by` in a real findings ledger, where "fixed" names the test that
proves it. Until then this catches the cheaper, commoner failure: two documents
that disagree.

THE ID-NAMESPACE TRAP. `GAP-CLOSURE-PLAN.md` uses G1..G26 for planned work;
`framework-map.yaml` uses G1..G9 for compliance gaps. **Eight ids collide and
mean different things** — plan-G2 is container resource limits, framework-G2 is
not. Comparing them would produce confident nonsense, so this refuses to and
reports the collision itself as a defect.

    python tests/findings_crosscheck_test.py
"""
import io, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TM = os.path.join(ROOT, "docs", "security", "THREAT-MODEL.md")
PLAN = os.path.join(ROOT, "docs", "plans", "GAP-CLOSURE-PLAN.md")
FMAP = os.path.join(ROOT, "compliance", "framework-map.yaml")

# Threat-model ids are ONE namespace across the whole repo: C/H/M/L + digits.
TM_ID = re.compile(r"\b([CHML]\d+)\b")

# Normalised statuses. "partial" is deliberately its own value rather than
# collapsing to closed — M3 and M4 were both wrong precisely because a half-fix
# was recorded as a fix.
CLOSED_WORDS = ("fixed", "closed", "resolved", "mitigated")
OPEN_WORDS = ("open",)
PARTIAL_WORDS = ("partial", "half", "partly")

FAILS = []
NOTES = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def read(p):
    return io.open(p, encoding="utf-8", errors="replace").read() if os.path.isfile(p) else ""


def _has(word, text):
    """WORD-BOUNDARY match, not substring. `"open" in "re-opens"` is true and
    meant the opposite: a line reading "a separate service re-opens C2" was read
    as "C2 is open". Substring matching on prose is the same defect that made
    "the suite" fire on every mention of a test suite."""
    # Built by concatenation rather than an f-string with escapes. Writing this
    # line through a shell heredoc turned the boundary escapes into literal
    # BACKSPACE bytes, so the pattern matched nothing, every status parsed as
    # None, and the suite reported "all cross-checks passed" while checking
    # zero things. A vacuous pass, inside the gate written to catch vacuous
    # claims. Found only by reintroducing a known contradiction and watching
    # it NOT fail.
    return re.search(r"\b" + re.escape(word) + r"\b", text) is not None


def classify(text):
    """Normalise a status blob. PARTIAL wins over CLOSED: a finding that says
    'half fixed' is not fixed, and treating it as such is the exact error that
    let M3 and M4 sit wrong."""
    t = text.lower()
    if any(_has(w, t) for w in PARTIAL_WORDS):
        return "partial"
    if any(_has(w, t) for w in CLOSED_WORDS):
        return "closed"
    if any(_has(w, t) for w in OPEN_WORDS):
        return "open"
    return None


def tm_statuses():
    """id -> status, from the threat model. Findings appear two ways: as `###`
    headings with a `· STATUS` suffix, and as `- **M3 — ...**` bullets whose
    status is stated inline."""
    out = {}
    for line in read(TM).splitlines():
        m = re.match(r"^###\s+([CHML]\d+)\s+—\s+(.*)$", line)
        if m:
            out[m.group(1)] = classify(m.group(2)) or "open"
            continue
        m = re.match(r"^-\s+\*\*([CHML]\d+)\s+—\s+(.*)$", line)
        if m:
            out[m.group(1)] = classify(m.group(2)) or "open"
    return out


def refs_with_status(path):
    """Threat-model ids referenced in another document, with the status that
    document ascribes to them. Only lines that BOTH name an id and state a
    status count — a passing mention is not a claim."""
    out = {}
    for line in read(path).splitlines():
        ids = set(TM_ID.findall(line))
        if not ids:
            continue
        st = classify(line)
        if st is None:
            continue
        # A line naming SEVERAL findings and ONE status is ambiguous — which
        # finding is the status about? "C1 keeps its place at the front of the
        # deep work (C2 closed 2026-07-28)" states a fact about C2 and says
        # nothing about C1, but a naive reading assigns "closed" to both and
        # invents a contradiction. That false positive is how a gate earns a
        # reputation for crying wolf and gets switched off, so ambiguity is
        # skipped rather than guessed.
        if len(ids) != 1:
            continue
        out.setdefault(next(iter(ids)), set()).add(st)
    return out


print("Threat-model findings parse at all:")
tm = tm_statuses()
check("findings were found", len(tm) >= 5, f"parsed {len(tm)}")
print(f"    {len(tm)} finding(s): " + ", ".join(f"{k}={v}" for k, v in sorted(tm.items())))

print("\nEvery threat-model id referenced elsewhere actually exists:")
for path, label in ((PLAN, "GAP-CLOSURE-PLAN.md"), (FMAP, "framework-map.yaml")):
    referenced = set()
    for line in read(path).splitlines():
        # Only count ids stated as threat-model references, not bare letters in
        # prose. "Threat-model M7" / "M7 —" / "(M7)" are claims; "L1" inside a
        # word is not.
        if re.search(r"[Tt]hreat[- ]model\s+([CHML]\d+)", line):
            referenced |= set(re.findall(r"[Tt]hreat[- ]model\s+([CHML]\d+)", line))
    unknown = {r for r in referenced if r not in tm}
    check(f"{label} references only known findings", not unknown,
          f"unknown id(s) {sorted(unknown)} — renamed or invented")

print("\nNo document contradicts the threat model about a finding's status:")
for path, label in ((PLAN, "GAP-CLOSURE-PLAN.md"),):
    for fid, claims in sorted(refs_with_status(path).items()):
        if fid not in tm:
            continue
        truth = tm[fid]
        # A contradiction is only closed-vs-open. partial-vs-anything is a
        # nuance mismatch, reported but not failed: the plan legitimately says
        # "resolved" about the half it owns.
        bad = {c for c in claims if {c, truth} == {"closed", "open"}}
        if bad:
            check(f"{label} agrees with THREAT-MODEL on {fid}", False,
                  f"threat model says {truth!r}, this doc says {sorted(bad)}")
        elif claims != {truth}:
            NOTES.append(f"{label} {fid}: threat model {truth!r} vs {sorted(claims)} (nuance, not a contradiction)")
    check(f"{label} has no closed/open contradiction", True) if not FAILS else None

print("\nThe two G-id namespaces are not silently conflated:")
plan_g = set(re.findall(r"\|\s*\*{0,2}(G\d+)\*{0,2}", read(PLAN)))
fmap_g = set(re.findall(r"^\s*-\s*id:\s*(G\d+)", read(FMAP), re.M))
collide = plan_g & fmap_g
# This is NOT a failure — both files are internally fine. It is a hazard that
# must stay visible, because any future cross-check that treats G-ids as one
# namespace will compare unrelated things and sound certain about it.
print(f"    plan G-ids: {len(plan_g)} · framework-map G-ids: {len(fmap_g)} · colliding: {len(collide)}")
check("the collision is documented where someone would trip on it",
      "different" in read(FMAP).lower() or "not the same" in read(FMAP).lower()
      or "namespace" in read(FMAP).lower(),
      f"{len(collide)} ids ({sorted(collide, key=lambda x: int(x[1:]))}) mean different things in "
      "the two files and nothing says so — add a note to framework-map.yaml's gaps section")

if NOTES:
    print("\nNuance mismatches (reported, not failed):")
    for n in NOTES:
        print("  note  " + n)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All finding cross-checks passed.")
