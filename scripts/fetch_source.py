#!/usr/bin/env python3
"""Fetch a page to a FILE and quote from it. Never summarises. That is the point.

WHY THIS EXISTS. Claude Code's `WebFetch` tool answers a prompt about a page
using a small fast model — its own tool description says so — so what reaches the
main model is that model's answer, not the page. For a "which way to build it"
question that is usually fine. For a citation with a number in it, it is a
compression step with no error bar, and a fabricated statistic is indistinguishable
from a real one once it is prose.

The failure is not really WebFetch's. It belongs to SUMMARISATION, so handing the
job to a subagent moves it one level up rather than fixing it: a subagent returns
prose too. What closes it is making the fetch return an ARTIFACT instead of a
conclusion — raw text on disk, a path, and a line number — so every claim can be
checked with a grep rather than a judgement call.

That is the same contract as `security/findings.yaml` and
`reliability/recurrence.yaml`, applied to research instead of controls: a claim
nobody can quote is not a verified claim.

    python scripts/fetch_source.py get <url> [--name slug]
    python scripts/fetch_source.py quote <slug> <regex> [--context N]
    python scripts/fetch_source.py list

Output goes to research/raw/<slug>.{html,txt,meta.json} — gitignored, because it
is somebody else's content cached locally as evidence, not source. Cite the URL,
the date and the quoted line; the file is what lets you check it again later.

IT WILL NOT ALWAYS WORK, and says so rather than returning something worse. A
JavaScript-rendered page yields a shell with no prose, and `get` warns when the
text-to-html ratio says that happened. A page behind auth or a bot check returns
its status code. Both are better outcomes than a confident summary of a page
nobody read.

Stdlib only.
"""
from __future__ import annotations
import argparse, html, json, os, re, shutil, sys, urllib.error, urllib.request
import datetime as _dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "research", "raw")
UA = "Mozilla/5.0 (compatible; ai-control-plane research fetch; +local)"

# Everything that is markup, navigation or code rather than the text of the page.
DROP = re.compile(r"<(script|style|noscript|svg|head)\b.*?</\1>", re.S | re.I)
BLOCK = re.compile(r"</(p|div|li|tr|h[1-6]|section|article|table|pre|blockquote)>", re.I)


def slugify(url):
    s = re.sub(r"^https?://", "", url)
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:80] or "page"


def to_text(raw_html):
    t = DROP.sub(" ", raw_html)
    t = BLOCK.sub("\n", t)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in t.splitlines()]
    # Collapse runs of blank lines but KEEP line structure: the line number is the
    # citation, so it has to be stable and meaningful.
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip() + "\n"


def cmd_get(args):
    os.makedirs(RAW, exist_ok=True)
    slug = args.name or slugify(args.url)
    # CURL FIRST, urllib second. Not a preference — this machine's Python has an
    # expired CA bundle and fails every https fetch with CERTIFICATE_VERIFY_FAILED,
    # while curl uses the OS trust store and works. The tempting fix is
    # ssl._create_unverified_context, which trades a fetch failure for silently
    # trusting anything: a research tool that cannot tell a real source from a
    # spoofed one is worse than one that says it could not fetch.
    body, status, final, how = None, None, args.url, None
    if shutil.which("curl"):
        import subprocess
        hp_tmp = os.path.join(RAW, "." + slug + ".part")
        r = subprocess.run(["curl", "-sSL", "-m", "45", "-A", UA,
                            "-w", "%{http_code}\t%{url_effective}",
                            "-o", hp_tmp, args.url],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode == 0 and os.path.isfile(hp_tmp):
            parts = (r.stdout or "").strip().split("\t")
            status = int(parts[0]) if parts and parts[0].isdigit() else 0
            final = parts[1] if len(parts) > 1 else args.url
            body = open(hp_tmp, "rb").read()
            os.remove(hp_tmp)
            how = "curl"
            if status >= 400:
                print(f"  HTTP {status} — not fetched. Nothing should be cited "
                      f"from this URL.")
                return 1
        else:
            print(f"  curl failed: {(r.stderr or '').strip()[:160]}")
    if body is None:
        req = urllib.request.Request(args.url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                body, status, final, how = r.read(), r.status, r.geturl(), "urllib"
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code} — not fetched. Nothing was written, and nothing "
                  f"should be cited from this URL.")
            return 1
        except (urllib.error.URLError, OSError) as e:
            print(f"  {type(e).__name__}: {e} — not fetched.")
            return 1

    charset = "utf-8"
    m = re.search(rb'charset=["\']?([\w-]+)', body[:4000], re.I)
    if m:
        charset = m.group(1).decode("ascii", "replace")
    raw = body.decode(charset, errors="replace")
    text = to_text(raw)

    hp = os.path.join(RAW, slug + ".html")
    tp = os.path.join(RAW, slug + ".txt")
    open(hp, "w", encoding="utf-8", newline="\n").write(raw)
    open(tp, "w", encoding="utf-8", newline="\n").write(text)
    meta = {"url": args.url, "final_url": final, "status": status, "via": how,
            "fetched": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "html_chars": len(raw), "text_chars": len(text),
            "text_lines": text.count("\n")}
    open(os.path.join(RAW, slug + ".meta.json"), "w", encoding="utf-8",
         newline="\n").write(json.dumps(meta, indent=2) + "\n")

    ratio = len(text) / max(len(raw), 1)
    print(f"  {slug}")
    print(f"    {final}")
    print(f"    HTTP {status} · {len(raw):,} html chars -> {len(text):,} text chars "
          f"({ratio:.1%}) · {meta['text_lines']:,} lines")
    print(f"    text: {os.path.relpath(tp, ROOT)}")
    # A JS-rendered page yields markup and no prose. Saying so beats quoting a nav bar.
    if len(text) < 1500 or ratio < 0.01:
        print("    WARNING: very little prose for this much markup — likely a "
              "JavaScript-rendered\n             page. Do not cite it; fetch the "
              "underlying API or a static mirror instead.")
    return 0


def cmd_quote(args):
    tp = os.path.join(RAW, args.slug + ".txt")
    if not os.path.isfile(tp):
        print(f"  no such fetched source: {args.slug} (run `get` first)")
        return 1
    lines = open(tp, encoding="utf-8", errors="replace").read().splitlines()
    rx = re.compile(args.pattern, re.I)
    hits = 0
    for i, ln in enumerate(lines, 1):
        if rx.search(ln):
            hits += 1
            lo, hi = max(1, i - args.context), min(len(lines), i + args.context)
            for j in range(lo, hi + 1):
                mark = ">>" if j == i else "  "
                print(f"  {mark} {args.slug}.txt:{j}: {lines[j-1][:300]}")
            print()
    # NOT PRESENT is a result, and a useful one. It is the answer that stops a
    # claim from being written at all, so it gets said plainly.
    print(f"  {hits} line(s) matched {args.pattern!r}"
          if hits else f"  NOT PRESENT — {args.pattern!r} does not appear in {args.slug}.txt")
    return 0 if hits else 2


def cmd_list(args):
    if not os.path.isdir(RAW):
        print("  nothing fetched yet")
        return 0
    for f in sorted(os.listdir(RAW)):
        if f.endswith(".meta.json"):
            m = json.load(open(os.path.join(RAW, f), encoding="utf-8"))
            print(f"  {f[:-10]:<52} {m['fetched'][:10]}  {m['text_chars']:>7,} chars  {m['url'][:60]}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get", help="fetch a URL to research/raw/")
    g.add_argument("url")
    g.add_argument("--name")
    q = sub.add_parser("quote", help="grep a fetched source, with line numbers to cite")
    q.add_argument("slug")
    q.add_argument("pattern")
    q.add_argument("--context", type=int, default=1)
    sub.add_parser("list", help="what has been fetched")
    a = ap.parse_args(argv)
    return {"get": cmd_get, "quote": cmd_quote, "list": cmd_list}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
