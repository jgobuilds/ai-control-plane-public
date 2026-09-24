#!/usr/bin/env python3
"""The three copies of the PII patterns must stay byte-identical.

`generated-artifacts.md`: derivatives drift silently. This one drifts into a
security hole rather than a stale document — if the router's copy learns a
pattern the runners' copies do not, the router refuses a prompt that a runner
accepts, and the difference surfaces only as an inconsistency nobody can
reproduce.

Vendored rather than imported because each service is its own Docker build
context, so a shared parent path does not exist at build time. A vendored copy
without a drift test is just a copy.

    python tests/pii_vendor_test.py
"""
import hashlib, io, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANONICAL = "router/pii-patterns.js"
COPIES = ["claude-runner/pii-patterns.js", "gemini-runner/pii-patterns.js"]

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def sha(rel):
    p = os.path.join(ROOT, *rel.split("/"))
    if not os.path.isfile(p):
        return None
    # Compare CONTENT, normalising line endings only. A CRLF checkout must not
    # read as tampering, but a changed pattern must.
    data = io.open(p, "rb").read().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


print("Every service scans for PII with the SAME patterns:")
base = sha(CANONICAL)
check(f"canonical exists ({CANONICAL})", base is not None)
for c in COPIES:
    got = sha(c)
    check(f"{c} exists", got is not None)
    if got is not None and base is not None:
        check(f"{c} is byte-identical to the canonical copy", got == base,
              f"{got[:12]} != {base[:12]} — re-vendor from {CANONICAL}")

print("\nNobody re-declares the patterns privately:")
# A service that keeps its own inline copy would pass the file check above while
# using different rules at run time. The router did exactly this until 2026-07-28.
for svc in ("router/server.js", "claude-runner/server.js", "gemini-runner/server.js"):
    p = os.path.join(ROOT, *svc.split("/"))
    src = io.open(p, encoding="utf-8", errors="replace").read()
    check(f"{svc} has no inline PII_PATTERNS block",
          not re.search(r"^\s*const PII_PATTERNS\s*=\s*\[", src, re.M),
          "declares its own patterns instead of requiring the vendored module")
    check(f"{svc} requires the vendored module",
          'require("./pii-patterns.js")' in src)

print("\nThe file ships in the image (a require of a missing file crash-loops):")
for df, svc in (("claude-runner/Dockerfile", "claude-runner"),
                ("gemini-runner/Dockerfile", "gemini-runner")):
    src = io.open(os.path.join(ROOT, *df.split("/")), encoding="utf-8").read()
    copies_all = re.search(r"^COPY\s+\.\s", src, re.M)
    check(f"{df} COPYs pii-patterns.js",
          bool(copies_all) or "pii-patterns.js" in src,
          "the Dockerfile lists files explicitly and would omit it")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All PII-vendoring checks passed.")
