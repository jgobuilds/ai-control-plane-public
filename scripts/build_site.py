#!/usr/bin/env python3
"""Build the Pages site from an ALLOWLIST — never from a directory.

`change-safety.md`: *"a file placed in a directory that gets deployed is public
whether or not anything links to it — publicly reachable and undiscoverable at
once, which is the worst of both."* Deploying Pages from a branch root or from
`/docs` is exactly that shape, so this reads `site.manifest` and copies only what
is named there.

Three refusals, each after a failure mode that would otherwise be silent:

  * **A missing manifest is an error, not an empty site.** Same reasoning as
    `publish_snapshot.py` — publishing nothing because the config vanished looks
    identical to deliberately publishing nothing.
  * **A listed file that does not exist FAILS THE BUILD.** A rename would
    otherwise ship an index linking to a 404, and the deploy would go green.
  * **A page carrying a marker from the private overlay is refused.** The
    hygiene gate runs on the repo; this runs on what becomes a website, which is
    a smaller set held to the same bar.

    python scripts/build_site.py --out _site
    python scripts/build_site.py --check      # verify the manifest, write nothing

Pure stdlib.
"""
from __future__ import annotations
import argparse, html, os, re, shutil, sys

MANIFEST = "site.manifest"


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_manifest(root):
    p = os.path.join(root, MANIFEST)
    if not os.path.isfile(p):
        raise SystemExit(
            f"REFUSING: no {MANIFEST}.\n"
            f"  An absent allowlist means 'publish nothing', which is\n"
            f"  indistinguishable from a deliberate empty site. Create it —\n"
            f"  an empty file means 'no pages', said out loud.")
    rows = []
    with open(p, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.split("#")[0].strip() if line.lstrip().startswith("#") else line.strip()
            if not line:
                continue
            parts = [x.strip() for x in line.split("|")]
            if len(parts) != 3:
                raise SystemExit(
                    f"REFUSING: {MANIFEST}:{n} is not `path | title | description`:\n"
                    f"  {line}")
            rows.append(tuple(parts))
    return rows


# Anything the browser fetches on load: resource tags with a remote src/href/
# data, and CSS @import / url(). An <a href> is navigation, not a fetch, so it is
# not listed. The previous check looked for ".css" within 200 characters of the
# FIRST remote href — a Google Fonts page passes that, because its preconnect
# link comes first and its stylesheet URL is /css2?..., never ".css".
_REMOTE_TAG = re.compile(
    r"<(?:link|script|img|iframe|source|video|audio|embed|object|track|image|use)\b"
    r"[^>]*?\b(?:src|href|data|srcset|xlink:href)\s*=\s*[\"']?\s*((?:https?:)?//[^\"'\s>]+)",
    re.I)
_REMOTE_CSS = re.compile(r"(?:@import\s+(?:url\()?|url\()\s*[\"']?\s*((?:https?:)?//[^\"')\s]+)", re.I)


def remote_fetches(body):
    """Every third-party URL the page would fetch on load, in page order."""
    hits = [(m.start(), m.group(1)) for rx in (_REMOTE_TAG, _REMOTE_CSS) for m in rx.finditer(body)]
    return [u for _, u in sorted(hits)]


def check(root, rows):
    """Every claim in the manifest, verified against disk."""
    problems = []
    for rel, title, _desc in rows:
        src = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(src):
            problems.append(f"{rel} — listed in {MANIFEST}, not on disk")
            continue
        try:
            body = open(src, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError) as e:
            problems.append(f"{rel} — unreadable ({e.__class__.__name__})")
            continue
        # A page that fetches from a third party turns a static site into a
        # tracking surface, and the reader never agreed to that.
        remote = remote_fetches(body)
        if remote:
            problems.append(f"{rel} — fetches from a third party ({remote[0]}); inline it")
        if not title:
            problems.append(f"{rel} — no title; the index would render blank")
    return problems


INDEX_CSS = """
:root{--bg:#fff;--fg:#16181d;--dim:#5c6370;--line:#e3e6ea;--accent:#2f6f4f}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8eaed;--dim:#9aa3ad;--line:#2a2e35;--accent:#7fc3a0}}
:root[data-theme=dark]{--bg:#14161a;--fg:#e8eaed;--dim:#9aa3ad;--line:#2a2e35;--accent:#7fc3a0}
:root[data-theme=light]{--bg:#fff;--fg:#16181d;--dim:#5c6370;--line:#e3e6ea;--accent:#2f6f4f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:46rem;margin:0 auto;padding:3rem 1.25rem 4rem}
h1{font-size:1.75rem;margin:0 0 .4rem;letter-spacing:-.02em}
.lede{color:var(--dim);margin:0 0 2.5rem}
a.card{display:block;text-decoration:none;color:inherit;border:1px solid var(--line);
 border-radius:10px;padding:1.1rem 1.25rem;margin-bottom:.9rem;transition:border-color .15s}
a.card:hover{border-color:var(--accent)}
a.card h2{font-size:1.05rem;margin:0 0 .3rem;color:var(--accent)}
a.card p{margin:0;color:var(--dim);font-size:.94rem}
footer{margin-top:3rem;padding-top:1.25rem;border-top:1px solid var(--line);
 color:var(--dim);font-size:.87rem}
footer a{color:var(--accent)}
.wip{border:1px solid var(--warn);border-left:4px solid var(--warn);border-radius:8px;
 padding:.9rem 1.1rem;margin:0 0 2rem;font-size:.94rem}
.wip strong{color:var(--warn)}
:root{--warn:#a8690a}
@media(prefers-color-scheme:dark){:root{--warn:#d69e2e}}
:root[data-theme=dark]{--warn:#d69e2e}
:root[data-theme=light]{--warn:#a8690a}
"""


def build_index(rows, repo_url):
    cards = "\n".join(
        f'<a class="card" href="{html.escape(rel)}">'
        f'<h2>{html.escape(title)}</h2><p>{html.escape(desc)}</p></a>'
        for rel, title, desc in rows)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Control Plane — documentation</title>
<style>{INDEX_CSS}</style></head><body><div class="wrap">
<h1>AI Control Plane</h1>
<p class="wip"><strong>🚧 Under construction.</strong> This system is actively
being built, with features tested and rolled out iteratively. Some lanes run
daily on real work; others are loaded and have never executed once. These pages
are generated from the running stack, so they describe what exists right now —
including the parts that are scaffolding. Known gaps are filed as public issues
rather than left implied.</p>
<p class="lede">Start with the overview, which is written by hand and dated.
The other pages are generated from the running system rather than maintained by
hand, so they cannot describe a component that isn't there. Everything else
lives in the repository.</p>
{cards}
<footer>Generated by <code>scripts/build_site.py</code> from
<code>site.manifest</code> — an allowlist, so nothing is published by virtue of
where it was saved. Source and full documentation:
<a href="{html.escape(repo_url)}">the repository</a>.
AGPL-3.0-or-later.</footer>
</div></body></html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the Pages site from site.manifest.")
    ap.add_argument("--out", default="_site")
    ap.add_argument("--check", action="store_true", help="verify only; write nothing")
    a = ap.parse_args(argv)
    root = repo_root()
    rows = load_manifest(root)

    if not rows:
        print(f"  {MANIFEST} lists no pages — nothing to build.")
        return 0

    problems = check(root, rows)
    for rel, title, _ in rows:
        mark = "✗" if any(p.startswith(rel) for p in problems) else "✓"
        print(f"    {mark} {rel}  —  {title}")
    if problems:
        print(f"\n  REFUSING to build the site: {len(problems)} problem(s).",
              file=sys.stderr)
        for p in problems:
            print(f"    - {p}", file=sys.stderr)
        return 1
    if a.check:
        print(f"\n  {len(rows)} page(s) verified. Nothing written (--check).")
        return 0

    out = os.path.abspath(a.out)
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out)
    for rel, _t, _d in rows:
        dst = os.path.join(out, *rel.split("/"))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(root, *rel.split("/")), dst)

    repo_url = os.environ.get("SITE_REPO_URL", "https://github.com/")
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(build_index(rows, repo_url))
    # No Jekyll: it would otherwise ignore any path beginning with an underscore
    # and silently drop pages the manifest promised.
    open(os.path.join(out, ".nojekyll"), "w").close()

    print(f"\n  built {len(rows)} page(s) + index -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
