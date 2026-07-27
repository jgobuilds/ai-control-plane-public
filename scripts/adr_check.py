#!/usr/bin/env python3
r"""ADR contract check — decisions must weigh cost, not just architecture.

`ai-standards/references/cost-awareness.md` says cost belongs IN the options table,
because cost in a separate paragraph gets read after the decision. This makes that
mechanical instead of aspirational: an ADR whose `Considered` table has no Cost
column, or has a blank Cost cell, fails here rather than being noticed a quarter
later. Same shape as model_policy_check — a rule enforced by a check that fails.

Deliberately narrow, so it stays credible:
  - only ADRs with a `## Considered` section that actually contains a table
  - a Cost column must exist, and no Cost cell may be empty
  - "unpriced" is a VALID answer (the lens says so) — blank is not

    python scripts/adr_check.py

Pure stdlib. Exit 1 on any failure.
"""
import os, re, sys, glob

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADR_DIR = os.path.join(ROOT, "docs", "decisions")
SKIP = {"TEMPLATE.md"}


def split_row(line):
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return cells


def check_file(path):
    """Return a list of failure strings for one ADR."""
    name = os.path.basename(path)
    text = open(path, encoding="utf-8").read()
    fails = []

    m = re.search(r"^##\s+Considered\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
    if not m:
        return []                      # no Considered section — nothing to enforce
    section = m.group(1)

    lines = [l for l in section.splitlines() if l.strip().startswith("|")]
    if not lines:
        return []                      # prose-only comparison — out of scope

    header = split_row(lines[0])
    cost_idx = next((i for i, h in enumerate(header) if h.lower().startswith("cost")), None)
    if cost_idx is None:
        fails.append(f"{name}: Considered table has no Cost column "
                     f"(columns: {', '.join(header)})")
        return fails

    # Skip the header and the |---|---| separator row.
    for row in lines[1:]:
        cells = split_row(row)
        if all(set(c) <= set("-: ") for c in cells if c):
            continue
        if len(cells) <= cost_idx or not cells[cost_idx]:
            label = cells[0] if cells else "(row)"
            fails.append(f"{name}: blank Cost cell for option {label!r} "
                         f"— write 'unpriced — needs a costed spike' if unknown")
    return fails


def main():
    paths = [p for p in sorted(glob.glob(os.path.join(ADR_DIR, "*.md")))
             if os.path.basename(p) not in SKIP]
    if not paths:
        print("No ADRs found — nothing to check.")
        return 0

    all_fails = []
    for p in paths:
        f = check_file(p)
        all_fails += f
        print(f"  {'FAIL' if f else 'ok  '}  {os.path.basename(p)}")

    print()
    if all_fails:
        print(f"FAILED: {len(all_fails)} issue(s)")
        for f in all_fails:
            print("  - " + f)
        print("\nSee ai-standards/references/cost-awareness.md and "
              "docs/decisions/TEMPLATE.md.")
        return 1
    print(f"All {len(paths)} ADR(s) weigh cost in their options table.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
