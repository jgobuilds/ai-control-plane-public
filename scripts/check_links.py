#!/usr/bin/env python3
"""Doc link gate — every relative markdown link must resolve.

Walks all .md under the repo, resolves each relative `](path)` link against its
file's directory, and fails if any target is missing. Ignores URLs, mailto, and
pure #anchors. This is deterministic (a file either exists or it doesn't), so the
real repo IS the corpus: it passes today and fails the moment a move or rename
leaves a dangling link — exactly the change-safety failure ("a move leaves
dangling references") turned into a mechanism.

    python scripts/check_links.py        # exit 1 if any link is broken

Pure stdlib. Windows-friendly.
"""
import os, re, sys, posixpath

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root, portable
LINK = re.compile(r"\]\(([^)\s]+)\)")


def broken_links(root=ROOT):
    out = []
    outside = []
    for dirpath, _, filenames in os.walk(root):
        if ".git" in dirpath.replace("\\", "/").split("/"):
            continue
        for f in filenames:
            if not f.endswith(".md"):
                continue
            fp = os.path.join(dirpath, f)
            rel_file = os.path.relpath(fp, root).replace("\\", "/")
            d = posixpath.dirname(rel_file)
            with open(fp, encoding="utf-8") as fh:
                text = fh.read()
            for m in LINK.finditer(text):
                rel = m.group(1)
                if "://" in rel or rel.startswith(("#", "mailto:")):
                    continue
                pathpart = rel.split("#")[0]
                if not pathpart:
                    continue
                tgt = posixpath.normpath(posixpath.join(d, pathpart)) if d else posixpath.normpath(pathpart)
                # A link that climbs out of the repo (../../../ai-standards) can
                # never be checked here: CI clones THIS repo alone, and the whole
                # overlay design is a sibling directory that exists on a
                # developer's disk and nowhere else. Calling that "broken" makes
                # the gate fail for a link that is correct on the only machine it
                # is meant to work on — so it is reported as UNVERIFIABLE instead
                # of being either failed or silently dropped.
                if tgt.startswith("../") or tgt == "..":
                    outside.append(f"{rel_file}  ->  {rel}")
                    continue
                if not os.path.exists(os.path.join(root, *tgt.split("/"))):
                    out.append(f"{rel_file}  ->  {rel}")
    return out, outside


def main():
    bad, outside = broken_links()
    if outside:
        # Printed even on success. An unverifiable link is a real limitation of
        # this check, and hiding it would let the reader believe every link was
        # verified.
        print(f"OUTSIDE THIS REPO — not checkable here ({len(outside)}):")
        for o in outside:
            print("  " + o)
        print("  These resolve only where the sibling repo is on disk. CI clones")
        print("  this repo alone, so it can neither confirm nor deny them.")
        print()
    if bad:
        print(f"BROKEN LINKS: {len(bad)}")
        for b in bad:
            print("  " + b)
        return 1
    print(f"All in-repo relative markdown links resolve. ✓"
          + (f"  ({len(outside)} cross-repo link(s) unverified)" if outside else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
