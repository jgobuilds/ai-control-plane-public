#!/usr/bin/env python3
"""The solutions log — a solved problem written down while the context is fresh.

Format borrowed from `EveryInc/compound-engineering-plugin` (MIT), whose
`/ce-compound` skill had the idea first: *the first time you solve a problem
takes research; document it, and the next time is free.* The plugin itself was
rejected — see the adopting repo's ADR — but the shape was worth taking.

WHAT GOES HERE, AND WHAT DOES NOT. There is one rule and it decides every case:

    Does the problem announce itself in a LOG LINE?
      yes -> a known-cause ROW (ci-known-causes.json). Matchable, deterministic,
             gated. The machine finds it for you.
      no  -> a SOLUTION (here). Read by a human, or by an agent that went
             looking. Nothing can match it, so it has to be findable instead.

Some problems earn both: the row matches it, the solution explains it. Filing an
unmatchable problem as a row is how a cause table fills with rows that can never
fire — which is exactly what happened before this existed.

Global options come BEFORE the subcommand — argparse puts them on the parser,
not the subparser, and `solution.py check --dir X` is a usage error rather than
a default. Stated because the first version of this docstring got it wrong:

    python scripts/solution.py --dir docs/solutions new "n8n needs a restart"
    python scripts/solution.py --dir docs/solutions check      # CI: shape only
    python scripts/solution.py --dir docs/solutions index      # regenerate INDEX.md
    python scripts/solution.py --dir docs/solutions stale --days 365

CI CHECKS SHAPE, NOT PROSE. Frontmatter present, ids unique, required fields
non-empty. It does not grade the writing: a gate that argues about quality is a
gate that gets switched off, and then nothing is checked at all.

Pure stdlib.
"""
from __future__ import annotations
import argparse, datetime as _dt, os, re, sys

FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
REQUIRED = ("id", "title", "status", "last_verified")
STATUSES = ("solved", "worked-around", "open")

TEMPLATE = """---
id: {id}
title: {title}
status: solved            # solved | worked-around | open
last_verified: {today}
tags: []
# A matchable log signature? Then it also belongs in ci-known-causes.json.
signature: none
---

# {title}

## What happened

<!-- The symptom, as it presented. What did someone SEE? -->

## Why it happened

<!-- The actual cause. Ask why past the first answer, but stop when you leave
     the evidence — two grounded steps beat five speculative ones. -->

## What was tried that did NOT work

<!-- The most valuable section and the one most often skipped. A wrong
     hypothesis that looked right saves the next person the same detour, and it
     is the part nobody can reconstruct later. -->

## The fix

<!-- What was actually done. -->

## Prevention

<!-- The MECHANISM that stops recurrence — a gate, a test, a default, a deleted
     file. Not an intention: "be more careful" is not an answer. If no mechanism
     is apparent, say so plainly rather than writing a wish. -->

## How we would know it came back

<!-- What to look for. If the answer is "nothing", that is itself a finding. -->
"""


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "solution"


def parse(path):
    """(frontmatter dict, body). Missing frontmatter -> ({}, text)."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return {}, ""
    m = FM.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if line.strip().startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.split("#")[0].strip()
    return meta, text[m.end():]


def entries(d):
    if not os.path.isdir(d):
        return []
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".md") and f != "INDEX.md":
            p = os.path.join(d, f)
            meta, body = parse(p)
            out.append((f, meta, body))
    return out


def cmd_new(a):
    os.makedirs(a.dir, exist_ok=True)
    today = a.today or _dt.date.today().isoformat()
    sid = a.id or f"SOL-{today.replace('-', '')}-{slugify(a.title)[:32]}"
    path = os.path.join(a.dir, f"{slugify(a.title)}.md")
    if os.path.exists(path):
        print(f"already exists: {path}", file=sys.stderr)
        return 1
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(TEMPLATE.format(id=sid, title=a.title, today=today))
    print(f"  {path}\n  Write it now, while you still remember what you tried "
          f"that did not work — that section cannot be reconstructed later.")
    return 0


def cmd_check(a):
    rows, problems, ids = entries(a.dir), [], {}
    for fname, meta, body in rows:
        if not meta:
            problems.append(f"{fname}: no YAML frontmatter")
            continue
        for k in REQUIRED:
            if not meta.get(k):
                problems.append(f"{fname}: missing or empty '{k}'")
        st = meta.get("status")
        if st and st not in STATUSES:
            problems.append(f"{fname}: status {st!r} not one of {'|'.join(STATUSES)}")
        lv = meta.get("last_verified", "")
        if lv:
            try:
                _dt.date.fromisoformat(lv)
            except ValueError:
                problems.append(f"{fname}: last_verified {lv!r} is not YYYY-MM-DD")
        sid = meta.get("id")
        if sid:
            if sid in ids:
                problems.append(f"{fname}: duplicate id {sid!r} (also {ids[sid]})")
            ids[sid] = fname
    if problems:
        print(f"SOLUTIONS LOG — {len(problems)} shape problem(s):", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("\n  Shape only — nothing here grades the writing.", file=sys.stderr)
        return 1
    print(f"Solutions log: {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}, "
          f"shape valid. ✓")
    return 0


def cmd_index(a):
    rows = entries(a.dir)
    today = _dt.date.fromisoformat(a.today) if a.today else _dt.date.today()
    L = ["# Solutions", "",
         "<!-- GENERATED by scripts/solution.py index — do not hand-edit. -->", "",
         "Problems solved here, written while the context was fresh. A problem "
         "that announces itself in a log line belongs in the known-cause table "
         "instead — that one is matched for you; these have to be findable.", "",
         f"{len(rows)} entr{'y' if len(rows) == 1 else 'ies'}.", "",
         "| Solution | Status | Verified | Signature |", "|---|---|---|---|"]
    for fname, meta, _ in rows:
        lv = meta.get("last_verified", "—")
        try:
            if lv != "—" and (today - _dt.date.fromisoformat(lv)).days > a.days:
                lv += " ⚠️"
        except ValueError:
            pass
        sig = meta.get("signature", "none")
        sig = "—" if sig in ("none", "") else "`" + sig + "`"
        L.append(f"| [{meta.get('title', fname)}]({fname}) | "
                 f"{meta.get('status', '?')} | {lv} | {sig} |")
    out = "\n".join(L).rstrip() + "\n"
    p = os.path.join(a.dir, "INDEX.md")
    if a.check:
        cur = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        if cur != out:
            print("SOLUTIONS INDEX DRIFT — run `solution.py index` and commit.",
                  file=sys.stderr)
            return 1
        print("Solutions index current. ✓")
        return 0
    os.makedirs(a.dir, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    print(f"  {p}  ({len(rows)} entries)")
    return 0


def cmd_stale(a):
    today = _dt.date.fromisoformat(a.today) if a.today else _dt.date.today()
    old = []
    for fname, meta, _ in entries(a.dir):
        lv = meta.get("last_verified")
        try:
            age = (today - _dt.date.fromisoformat(lv)).days if lv else None
        except ValueError:
            age = None
        if age is not None and age > a.days:
            old.append((fname, age))
    if not old:
        print(f"No solution older than {a.days} days. ✓")
    else:
        # Reported, never enforced. A build that reddens because a date passed
        # teaches people to back-date the field, which is worse than no field.
        print(f"{len(old)} solution(s) past {a.days} days since verification "
              f"(reported, not failed):")
        for f, age in sorted(old, key=lambda x: -x[1]):
            print(f"  {age:5d}d  {f}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="The solutions log.")
    ap.add_argument("--dir", default="docs/solutions")
    ap.add_argument("--today", help="ISO date, for reproducible tests")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new"); n.add_argument("title"); n.add_argument("--id")
    sub.add_parser("check")
    i = sub.add_parser("index"); i.add_argument("--check", action="store_true")
    i.add_argument("--days", type=int, default=365)
    s = sub.add_parser("stale"); s.add_argument("--days", type=int, default=365)
    a = ap.parse_args(argv)
    return {"new": cmd_new, "check": cmd_check, "index": cmd_index,
            "stale": cmd_stale}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
