#!/usr/bin/env python3
"""Prove scripts/claims_check.py catches a retracted claim — offline, no git.

Each case is a mutation: text a real doc once contained (or a wrapped/bolded
variant of it) must be FOUND, and the corrected wording must NOT be. A check that
passes the corrected text but also passes the stale one checks nothing.

    python tests/claims_check_test.py
"""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import claims_check as cc  # noqa: E402

fails = []


def expect(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def main():
    reg = json.load(open(cc.REGISTRY, encoding="utf-8"))
    claim = next(c for c in reg["claims"] if c["id"] == "anthropic-agent-sdk-credit-cancelled")
    pats = claim["patterns"]

    print("Stale wording, as it actually appeared, is caught:")
    stale = {
        "README, wrapped in a blockquote":
            "> credit pool, then **cancelled it before it took effect** (2026-06-15),\n"
            "> confirming subscription limits are unchanged and promising advance notice",
        "LADDER, wrapped in a list item":
            "  credit pool, then **cancelled it on 2026-06-15 before it took effect**;\n"
            "  headless `claude -p` still draws on subscription limits today",
        "MARKET-ANALYSIS banner":
            "> to a metered credit pool on 2026-06-15\". **That change was cancelled on\n"
            "> 2026-06-15 before taking effect**, and the July sources",
        "'retracted' framing":
            "a vendor policy announced, retracted and under rework: the metered credit",
        "'does currently cover' with bold":
            "So a subscription **does** currently cover unattended volume",
    }
    for name, text in stale.items():
        expect(name, cc.find(text, pats), "no match")

    print("\nCorrected wording is not flagged:")
    for name, text in {
        "README now": "Since **2026-06-15**, headless `claude -p` on a Claude\n> subscription no longer "
                      "draws on plan usage limits: it draws on a per-user\n> **monthly Agent SDK credit**",
        "LADDER now": "rates, not on plan usage limits — and stops at the ceiling unless usage\n  credits are enabled.",
        "unrelated 'cancelled'": "The run was cancelled by the operator.",
    }.items():
        hits = cc.find(text, pats)
        expect(name, not hits, hits)

    print("\nLine numbers survive normalisation:")
    text = "line one\nline two\n> first half of a claim, then cancelled it\n> before it took effect.\n"
    hits = cc.find(text, pats)
    expect("a claim wrapped from line 3 reports line 3", hits and hits[0][0] == 3, hits)
    expect("normalise preserves length", len(cc.normalise(text)) == len(text))

    print("\nRegistry validation:")
    expect("the committed registry is valid", cc.registry_problems(reg) == [], cc.registry_problems(reg))
    broken = {"claims": [{**claim, "patterns": ["(unclosed"]}]}
    expect("a bad regex is refused", any("bad pattern" in p for p in cc.registry_problems(broken)))
    gone = {"claims": [{**claim, "history": ["no/such/file.md"]}]}
    expect("an absent history file is tolerated — the public snapshot excludes some",
           cc.registry_problems(gone) == [], cc.registry_problems(gone))
    lost = {"claims": [{**claim, "source_of_truth": "no/such/file.md"}]}
    expect("an absent source_of_truth is still refused",
           any("does not exist" in p for p in cc.registry_problems(lost)))
    thin = {"claims": [{k: v for k, v in claim.items() if k != "truth"}]}
    expect("a claim with no stated truth is refused", any("missing truth" in p for p in cc.registry_problems(thin)))
    expect("an empty registry is refused", cc.registry_problems({"claims": []}) != [])

    print()
    if fails:
        print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
        return 1
    print("All claims-check checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
