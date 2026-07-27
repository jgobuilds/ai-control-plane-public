#!/usr/bin/env python3
"""Verify the router's tamper-evident audit ledger.

Re-walks audit/decisions.jsonl and recomputes the hash chain. Any in-place edit,
deletion, or reorder breaks the chain and is reported with the offending line.
Language-agnostic by design: we re-hash the RAW json substring (never re-serialize),
so JS-written / Python-verified stays byte-identical.

Usage: python scripts/verify_audit.py [path]   (default: ./audit/decisions.jsonl)
"""
import sys, os, json, hashlib

def sha256(s): return hashlib.sha256(s.encode()).hexdigest()

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit", "decisions.jsonl")
    if not os.path.exists(path):
        print(f"no ledger at {path} (nothing to verify yet)"); return 0
    prev, n = "GENESIS", 0
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line: continue
            n += 1
            h, _, j = line.partition(" ")
            if sha256(prev + j) != h:
                print(f"TAMPER at line {i}: hash mismatch (chain broken)"); return 1
            rec = json.loads(j)
            if rec.get("prevHash") != prev:
                print(f"TAMPER at line {i}: prevHash mismatch"); return 1
            prev = h
    print(f"OK — {n} record(s), hash chain intact. Head: {prev[:16]}…")
    return 0

if __name__ == "__main__":
    sys.exit(main())
