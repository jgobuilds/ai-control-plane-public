#!/usr/bin/env python3
"""Corpus for the cloud-provider registry.

One property matters more than the rest: **an unbuilt provider must refuse, not
fall back.** A capability that silently degrades to nothing is how a control
plane comes to believe it tokenized something it did not — the same failure the
diagnostician's no-log tier exists to prevent, one layer down.

The second is honesty about the seam itself. A shape with one implementation is
an assumption wearing an interface, and the audit has to say so rather than
print a green tick over a single vendor.

    python tests/cloud_test.py
"""
import importlib.util, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "cloud", os.path.join(HERE, "..", "scripts", "cloud.py"))
cl = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cl)

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


REG = cl.load()

print("An unbuilt provider REFUSES rather than falling back:")
for cap, prov in (("transcription", "aws"), ("transcription", "azure"),
                  ("secrets", "google"), ("dlp", "aws")):
    try:
        cl.resolve(cap, prov, REG)
        check(f"{cap} via {prov} refuses", False, "it resolved — silent fallback")
    except cl.NotBuilt as e:
        msg = str(e)
        check(f"{cap} via {prov} refuses", True)
        if (cap, prov) == ("transcription", "aws"):
            check("the refusal names what IS built", "built" in msg and "google" in msg)
            check("and says why refusing beats falling back",
                  "silently does nothing" in msg)

print("\nA built provider resolves, and carries what a caller needs:")
rec = cl.resolve("transcription", "google", REG)
for f in ("status", "service", "egress", "processor", "notes"):
    check(f"transcription/google carries {f}", f in rec, str(sorted(rec)))
check("it names the identity property ADR 0015 turns on",
      "identity" in rec and "identity" in rec["identity"].lower(), str(rec.get("identity")))

print("\nStatus is never ambiguous:")
allowed = {"implemented", "stub", "unsupported"}
bad = [(c, p, r.get("status")) for c, cap in REG["capabilities"].items()
       for p, r in cap["providers"].items() if r.get("status") not in allowed]
check("every provider has a known status", bad == [], str(bad))
rows, tally = cl.audit(REG)
check("the audit counts every declared pair", len(rows) == sum(tally.values()))
# Nothing is 'implemented' yet, and the registry must not pretend otherwise.
check("nothing claims to be implemented while it is a stub",
      tally.get("implemented", 0) == 0,
      f"{tally.get('implemented', 0)} claim implemented — verify each before allowing it")

print("\nEvery provider declares its egress, because choosing one opens it:")
missing = [(c, p) for c, cap in REG["capabilities"].items()
           for p, r in cap["providers"].items() if not r.get("egress")]
check("no provider is silent about its egress", missing == [], str(missing))
eg = cl.egress(REG)
check("egress is grouped per provider", set(eg) >= {"google", "azure", "aws"}, str(sorted(eg)))
check("google's egress is not empty", bool(eg.get("google")))

print("\nEvery provider names the processor relationship it needs:")
# ADR 0010 rejects any processor without one for tenant work, so a provider
# that does not name it cannot be evaluated against that rule at all.
noproc = [(c, p) for c, cap in REG["capabilities"].items()
          for p, r in cap["providers"].items() if not r.get("processor")]
check("no provider omits its processor requirement", noproc == [], str(noproc))

print("\nUnknown names fail loudly rather than defaulting:")
for cap, prov in (("no-such-capability", None), ("transcription", "oracle")):
    try:
        cl.resolve(cap, prov, REG)
        check(f"{cap}/{prov} refuses", False, "it resolved")
    except cl.NotBuilt as e:
        check(f"{cap}/{prov} refuses", True)
        check(f"  and lists the valid choices for {cap}",
              ("Known:" in str(e) or "Declared:" in str(e)))

print("\nA broken registry is not an empty one:")
d = tempfile.mkdtemp()
p = os.path.join(d, "bad.json")
open(p, "w", encoding="utf-8").write("{not json")
try:
    cl.load(p)
    check("a malformed registry raises", False, "it loaded")
except ValueError:
    check("a malformed registry raises", True)

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All cloud-registry checks passed.")
