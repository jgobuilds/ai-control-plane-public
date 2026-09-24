#!/usr/bin/env python3
"""Prove scripts/build_site.py refuses a page that fetches from a third party.

The rule is in the site builder because a public page that loads a remote font,
script or image turns a static site into a tracking surface. The first version
of the check passed a Google Fonts page — it looked for ".css" near the first
remote href, and the fonts URL is /css2?... — so each case below is a real way
a page fetches on load, and must be caught.

    python tests/build_site_test.py
"""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import build_site as bs  # noqa: E402

fails = []


def expect(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def main():
    print("Fetches on load are caught:")
    caught = {
        "Google Fonts, preconnect first (the case the old check missed)":
            '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans&display=swap">',
        "remote script": '<script src="https://cdn.example.net/lib.min.js"></script>',
        "protocol-relative image": "<img alt='' src=//img.example.net/p.png>",
        "CSS @import": "<style>@import url('https://fonts.example.net/a.css');</style>",
        "CSS @import without url()": '<style>@import "https://fonts.example.net/a.css";</style>',
        "CSS url() background": "<style>body{background:url(https://t.example.net/px.gif)}</style>",
        "iframe embed": '<iframe src="https://www.example.net/embed/x"></iframe>',
        "srcset": '<img alt="" srcset="https://img.example.net/a.png 2x">',
    }
    for name, html in caught.items():
        expect(name, bs.remote_fetches(html), "not detected")

    print("\nThings that are not fetches pass:")
    for name, html in {
        "an outbound link": '<a href="https://github.com/">the repository</a>',
        "an inline data: font": "<style>@font-face{src:url(data:font/woff2;base64,AAAA)}</style>",
        "a relative stylesheet": '<link rel="stylesheet" href="site.css">',
        "a same-page anchor": '<a href="#lenses">How lenses work</a>',
        "a URL in prose": "<p>Fetched from https://support.example.net/article on 2026-09-19.</p>",
    }.items():
        hits = bs.remote_fetches(html)
        expect(name, not hits, hits)

    print("\nThe published pages themselves:")
    rows = bs.load_manifest(ROOT)
    problems = bs.check(ROOT, rows)
    expect(f"every page in site.manifest passes ({len(rows)})", not problems, problems)

    print()
    if fails:
        print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
        return 1
    print("All site-build checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
