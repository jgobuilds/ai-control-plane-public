#!/usr/bin/env python3
"""Retracted-claims check — a claim corrected once must not come back.

Fixing the source of truth does not retract a claim that other docs restate in
their own words. On 2026-08-10 docs/design/PROVIDER-BILLING.md was corrected
against Anthropic's Help Center; the README, LADDER and MARKET-ANALYSIS kept
saying the opposite for seven more weeks, while a dossier entry recorded that
"all four places now point at" the matrix. They pointed at it — and contradicted
it in the next sentence.

context/retracted-claims.json lists each retracted claim as regexes plus the
files allowed to quote it as history. Every tracked .md/.html file is scanned;
a match anywhere else fails.

Text is normalised before matching so a claim wrapped across lines still
matches: line breaks with their blockquote markers collapse to spaces, and `**`
is blanked — both same-length substitutions, so reported line numbers stay true.

    python scripts/claims_check.py

Pure stdlib. Exit 0 clean, 1 on a reappearance or a malformed registry.
"""
import json, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "context", "retracted-claims.json")
SCANNED = (".md", ".html")
SKIP_DIRS = ("research/raw/",)        # fetched vendor pages, not our claims
REQUIRED = ("id", "retracted", "truth", "source_of_truth", "verified", "patterns", "history")


def normalise(text):
    """Collapse wraps and bold without changing any character offsets."""
    text = re.sub(r"\n[ \t>]*", lambda m: " " * len(m.group(0)), text)
    return text.replace("**", "  ")


def find(text, patterns):
    """[(line, excerpt)] for every pattern match in text."""
    flat = normalise(text)
    hits = []
    for p in patterns:
        # A space in a pattern means any run of whitespace: blanking `**` and a
        # wrap both leave several spaces where the prose had one.
        for m in re.finditer(p.replace(" ", r"\s+"), flat, re.I):
            line = text.count("\n", 0, m.start()) + 1
            hits.append((line, " ".join(m.group(0).split())))
    return sorted(set(hits))


def registry_problems(reg):
    out = []
    for i, c in enumerate(reg.get("claims", [])):
        missing = [k for k in REQUIRED if not c.get(k)]
        if missing:
            out.append(f"claim #{i} ({c.get('id', '?')}) missing {', '.join(missing)}")
            continue
        for p in c["patterns"]:
            try:
                re.compile(p)
            except re.error as e:
                out.append(f"{c['id']}: bad pattern {p!r}: {e}")
        if not os.path.isfile(os.path.join(ROOT, c["source_of_truth"])):
            out.append(f"{c['id']}: source_of_truth {c['source_of_truth']} does not exist")
        # A `history` file that is absent is NOT an error: the public snapshot
        # publishes by allowlist, so files this repo excludes (idea-dossier/,
        # docs/plans/) are simply not there. An absent file cannot restate a
        # claim, so the allowance is vacuous rather than wrong — and a mistyped
        # path fails loudly instead, because the real file then gets flagged.
        # Watched failing the other way: requiring them broke the published
        # repo's CI on 2026-09-24 while this repo was green.
    if not reg.get("claims"):
        out.append("registry lists no claims")
    return out


def tracked_docs():
    r = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [f for f in r.stdout.splitlines()
            if f.endswith(SCANNED) and not f.startswith(SKIP_DIRS)]


def main():
    reg = json.load(open(REGISTRY, encoding="utf-8"))
    bad = registry_problems(reg)
    for b in bad:
        print(f"  REGISTRY  {b}")
    if bad:
        return 1

    files = tracked_docs()
    fails = 0
    for c in reg["claims"]:
        allowed = set(c["history"])
        for f in files:
            if f in allowed:
                continue
            text = open(os.path.join(ROOT, f), encoding="utf-8", errors="replace").read()
            for line, excerpt in find(text, c["patterns"]):
                fails += 1
                print(f"  FAIL  {f}:{line}  restates retracted claim '{c['id']}': \"{excerpt}\"")
                print(f"        truth ({c['verified']}): {c['truth']}  -> {c['source_of_truth']}")
    if fails:
        print(f"\n{fails} restatement(s) of a retracted claim. Correct the text, or — if it quotes the")
        print("claim as history on purpose — add the file to that claim's `history` list.")
        return 1
    print(f"PASS  {len(reg['claims'])} retracted claim(s), {len(files)} tracked doc(s), no restatements")
    return 0


if __name__ == "__main__":
    sys.exit(main())
