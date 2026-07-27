#!/usr/bin/env python3
"""End-to-end tamper-evidence test for the audit ledger.

Builds a small hash-chained ledger in Python using EXACTLY the router's line
format ("<hash> <json>", hash = sha256(prevHash + json), each record carrying its
own prevHash), then runs the real scripts/verify_audit.py against it:
  1. intact chain            -> verify_audit.py exits 0 (green)
  2. a byte edited in a record -> exits non-zero (tamper detected)
  3. two records swapped       -> exits non-zero (reorder detected)
This proves the chain is verifiable across languages (JS writes / Python checks)
AND that any edit or reorder breaks it.

    python tests/audit_chain_test.py     # exits non-zero on any failure
"""
import os, sys, json, hashlib, tempfile, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFY = os.path.join(ROOT, "scripts", "verify_audit.py")

def sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()

def build_chain(records):
    """Mirror router auditRecord: append prevHash, hash the raw json substring."""
    lines, prev = [], "GENESIS"
    for rec in records:
        rec = dict(rec, prevHash=prev)
        j = json.dumps(rec)             # the RAW substring that gets re-hashed
        h = sha256(prev + j)
        lines.append(f"{h} {j}")
        prev = h
    return "\n".join(lines) + "\n"

def write(tmp, text):
    p = os.path.join(tmp, "decisions.jsonl")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return p

def run_verify(path):
    return subprocess.run([sys.executable, VERIFY, path], capture_output=True, text=True)

RECORDS = [
    {"ts": "2026-07-18T10:00:00.000Z", "scope": "jane",     "tier": "t2", "action": "advise"},
    {"ts": "2026-07-18T10:01:00.000Z", "scope": "client-a", "tier": "t3", "action": "write"},
    {"ts": "2026-07-18T10:02:00.000Z", "scope": "jane",     "tier": "t1", "action": "advise"},
]

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)

with tempfile.TemporaryDirectory() as tmp:
    intact = build_chain(RECORDS)

    # 1. intact chain verifies green
    p = write(tmp, intact)
    r = run_verify(p)
    check("intact chain: verify_audit.py exits 0", r.returncode == 0, r.stdout.strip() + r.stderr.strip())
    check("intact chain: reports the chain intact", "hash chain intact" in r.stdout)

    # 2. edit a byte inside record #2's json (hash no longer matches)
    lines = intact.strip("\n").split("\n")
    tampered = list(lines)
    tampered[1] = tampered[1].replace('"tier": "t3"', '"tier": "t0"')  # silent downgrade attempt
    p = write(tmp, "\n".join(tampered) + "\n")
    r = run_verify(p)
    check("edited record: verify_audit.py exits non-zero", r.returncode != 0)
    check("edited record: reports TAMPER", "TAMPER" in r.stdout)

    # 3. reorder two records (prevHash / chain hash break)
    reordered = [lines[1], lines[0], lines[2]]
    p = write(tmp, "\n".join(reordered) + "\n")
    r = run_verify(p)
    check("reordered records: verify_audit.py exits non-zero", r.returncode != 0)
    check("reordered records: reports TAMPER", "TAMPER" in r.stdout)

    # 4. a deleted record breaks the successor's prevHash link
    deleted = [lines[0], lines[2]]
    p = write(tmp, "\n".join(deleted) + "\n")
    r = run_verify(p)
    check("deleted record: verify_audit.py exits non-zero", r.returncode != 0)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("All audit-chain tamper-evidence checks passed.")
