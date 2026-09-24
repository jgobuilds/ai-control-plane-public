#!/usr/bin/env python3
r"""Research watch — fetch what changed in the things this framework actually depends on.

WHY THIS SHAPE. The agent runners are firewalled default-deny: verified 2026-07-25,
github.com times out from claude-runner while api.anthropic.com returns 401
(reached, auth-rejected). That is the containment property working, and widening
the allowlist so an agent could browse would give it back. So this mirrors the
drive-sync pattern the framework already uses:

    FETCH (egress, here in `lanes`) -> workspace/research/ -> REASON (firewalled runner)

The runner never reaches the internet. It reads a file.

WHAT IT WATCHES, and why these and not "AI news": every source below is tied to a
DECISION WE ALREADY RECORDED. The point is not a feed; it is to notice when one of
our own ADR revisit-triggers has fired. A release that does not move a decision is
noise, and this deliberately does not report it.

Unauthenticated GitHub API: 60 req/hr, far above what this needs. No token, so
nothing to leak. Sources are a hardcoded allowlist — an application-level
restriction, weaker than the runners' network-level firewall, and stated plainly
rather than implied.

    python scripts/research_watch.py --days 30            # human summary
    python scripts/research_watch.py --days 30 --json     # for the lane
"""
from __future__ import annotations
import os, re, json, argparse, datetime, urllib.request, urllib.error

# repo -> (why we care, which recorded decision it could move)
WATCH = {
    "n8n-io/n8n": (
        "our orchestration runtime, and the highest-risk component in the stack",
        "image-policy reviewBy; a security release means re-pin NOW (2026 CVEs "
        "included CVSS 10.0 unauth RCE)"),
    "aquasecurity/trivy": (
        "the scanner both CI gates depend on",
        "image-policy reviewBy — a pinned scanner still needs its engine current"),
    "anthropics/claude-code": (
        "the CLI the runners actually execute",
        "model-policy reviewBy; behaviour changes affect every routed call"),
    "BerriAI/litellm": (
        "rejected for the privileged path on CVE record (ADR 0007 discussion)",
        "ADR 0001/0007 — a sustained clean security record would reopen it"),
    "langfuse/langfuse": (
        "deferred observability candidate",
        "ADR 0001 — adopt-vs-build on tracing"),
    "getzep/graphiti": (
        "the presumptive choice IF we ever adopt a memory layer",
        "ADR 0008 — revisit triggers incl. per-scope isolation support"),
}

# Watched repo -> the ref in context/image-policy.json we actually pin. A release
# feed is interesting; "you are two minor versions behind on the component with a
# CVSS 10.0 history" is ACTIONABLE, and it is the thing this tool exists to say.
PINNED_AS = {
    "n8n-io/n8n": "n8nio/n8n",
    "aquasecurity/trivy": "aquasec/trivy",
}

# What we RUN versus what we merely WATCH as a candidate. The distinction is not
# cosmetic: a security release in something we run is an operational event, while
# the same release in a deferred candidate is at most an input to a future
# decision. The first draft flattened both into "dependencies this system runs" —
# caught by the agent reading its own report, which is the loop working.
RUNNING = {"n8n-io/n8n", "aquasecurity/trivy", "anthropics/claude-code"}

API = "https://api.github.com/repos/{repo}/releases?per_page=5"
UA = {"User-Agent": "ai-control-plane-research-watch", "Accept": "application/vnd.github+json"}

# Signals that a release moves a DECISION rather than just shipping features.
#
# WORD-BOUNDARY REGEX, NOT SUBSTRINGS — learned immediately and embarrassingly:
# the first version matched "rce" as a substring and fired on EVERY release,
# because GitHub release bodies are full of HTML where "source" and "resource"
# both contain it. Every n8n release looked like an RCE advisory. A detector
# without a labeled corpus is unshipped (proving-controls.md); the corpus is now in
# tests/test_research_watch.py.
SIGNAL = {
    "security":      r"\bsecurity\b",
    "cve":           r"\bCVE-\d{4}-\d+\b",
    "vulnerability": r"\bvulnerabilit(?:y|ies)\b",
    # The word boundaries ARE the fix: without them, re.I made "RCE" match
    # inside "source" and "resource", so every release read as an advisory.
    "rce":           r"\bRCE\b|\bremote code execution\b",
    "auth-bypass":   r"\bauth(?:entication|orization)?[ -]bypass\b",
    "breaking":      r"\bbreaking change\b",
    "deprecation":   r"\bdeprecat(?:ed|ion|ing)\b",
    "license":       r"\blicen[sc]e change\b",
    "eol":           r"\bend[ -]of[ -]life\b|\bEOL\b",
}

TAG_RE = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://\S+")


def strip_markup(text):
    """Release bodies are HTML + markdown + link soup. Matching raw text is how
    'source' became an RCE report."""
    return URL_RE.sub(" ", TAG_RE.sub(" ", text or ""))


def signals_in(text):
    clean = strip_markup(text)
    return sorted(name for name, pat in SIGNAL.items()
                  if re.search(pat, clean, re.I))


def fetch(repo, timeout=15):
    try:
        req = urllib.request.Request(API.format(repo=repo), headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:                      # noqa: BLE001 — never let one source kill the run
        return None, f"{type(e).__name__}"


def pinned_versions(root=None):
    """What image-policy.json says we are running, so drift is measurable."""
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "context", "image-policy.json")
    try:
        pol = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {i["ref"]: i.get("version") for i in pol.get("images", []) if i.get("version")}


def analyse(days):
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    out, errors = [], []
    for repo, (why, decision) in WATCH.items():
        rels, err = fetch(repo)
        if err:
            errors.append(f"{repo}: {err}")
            continue
        for rel in rels or []:
            pub = rel.get("published_at") or ""
            try:
                when = datetime.datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when < cutoff:
                continue
            body = (rel.get("body") or "")[:4000]
            hits = signals_in((rel.get("name") or "") + " " + body)
            out.append({
                "repo": repo, "why": why, "decision": decision,
                "running": repo in RUNNING,
                "tag": rel.get("tag_name"), "published": pub[:10],
                "url": rel.get("html_url"),
                "signals": hits,
                "notable": bool(hits),
                "excerpt": " ".join(body.split())[:400],
            })
    # Version drift against what we actually run.
    pins = pinned_versions()
    for item in out:
        ref = PINNED_AS.get(item["repo"])
        if ref and pins.get(ref):
            item["pinned"] = pins[ref]
    out.sort(key=lambda r: (not r["notable"], r["published"]), reverse=False)
    return out, errors


VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def as_version(tag):
    """Parse a release tag into a comparable tuple, or None if it is not a version.

    Needed because projects publish ROLLING tags alongside real ones — n8n ships
    `stable` and `beta` on the same day as n8n@2.32.5. Picking the newest by DATE
    reported 'released stable', which is not a version and cannot be compared to
    a pin. Pick the highest VERSION instead, and ignore tags that are not one.
    """
    m = VERSION_RE.search(tag or "")
    return tuple(int(g) for g in m.groups()) if m else None


def drift(items):
    """Highest released version per pinned repo vs the version we actually run."""
    rows = []
    for repo, ref in PINNED_AS.items():
        versioned = [(as_version(i["tag"]), i) for i in items
                     if i["repo"] == repo and i.get("pinned")]
        versioned = [(v, i) for v, i in versioned if v]
        if not versioned:
            continue
        top_v, top = max(versioned, key=lambda vi: vi[0])
        pinned_v = as_version(top["pinned"])
        # Only report when the release is genuinely NEWER — a pin ahead of the
        # feed (a pre-release, say) is not drift and must not nag.
        if pinned_v and top_v > pinned_v:
            rows.append({"repo": repo, "pinned": top["pinned"],
                         "latest": ".".join(str(n) for n in top_v),
                         "published": top["published"],
                         "behind": f"{top_v[0]-pinned_v[0]} major, "
                                   f"{top_v[1]-pinned_v[1]} minor"})
    return rows


def report(days):
    """The one payload shape, shared by the CLI and the lanes HTTP endpoint.

    Both callers must see the SAME facts — a summary that differs by transport is
    a summary nobody can trust.
    """
    items, errors = analyse(days)
    notable = [i for i in items if i["notable"]]
    version_drift = drift(items)
    return {
        "window_days": days,
        "sources": len(WATCH),
        "releases": len(items),
        "notable": len(notable),
        # Distinguish "checked and quiet" from "could not check". The lane branches
        # on this, and a failed fetch must never read as a clean bill of health.
        "checked_all_sources": not errors,
        "unreachable": errors,
        "version_drift": version_drift,
        # Anything for a human or an agent to act on? Drives whether the lane
        # spends a model call at all.
        "actionable": bool(notable or version_drift or errors),
        "items": items,
    }


def main():
    ap = argparse.ArgumentParser(description="Watch releases that could move a recorded decision.")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args()

    # Fetch ONCE. An earlier draft called analyse() and then report(), which
    # double-hit every source for a single run — cheap here, but the kind of
    # accidental amplification that gets a scheduled job rate-limited.
    rep = report(a.days)
    items, errors = rep["items"], rep["unreachable"]
    notable = [i for i in items if i["notable"]]

    if a.as_json:
        print(json.dumps(rep, indent=2))
        return 0

    print(f"Research watch — {len(WATCH)} sources, last {a.days} days\n")
    if errors and len(errors) == len(WATCH):
        # "Nothing changed" and "nothing could be checked" are opposite facts and
        # must never render the same. Reporting quiet over a failed fetch is the
        # silent-empty-result failure this framework has a whole lens about.
        print("  NOTHING WAS CHECKED — every source was unreachable (see below).")
        print("  This is NOT 'no releases'. Draw no conclusion from it.")
    elif not items:
        print("  No releases in the window. That is a finding, not a gap: nothing")
        print("  we depend on has moved, so no recorded decision needs revisiting.")
    for i in items:
        flag = "NOTABLE" if i["notable"] else "       "
        print(f"  [{flag}] {i['repo']} {i['tag']}  ({i['published']})")
        if i["signals"]:
            print(f"            signals: {', '.join(i['signals'])}")
            print(f"            could move: {i['decision']}")
    if errors:
        # Never silently drop a source — an unreachable feed is not a quiet feed.
        print(f"\n  UNREACHABLE ({len(errors)}) — these were NOT checked:")
        for e in errors:
            print(f"    {e}")
    d = drift(items)
    if d:
        print("\n  VERSION DRIFT — what we PIN vs what has shipped:")
        for r in d:
            print(f"    {r['repo']}: pinned {r['pinned']}  ->  released {r['latest']} ({r['published']})")
        print("    Re-pin deliberately in context/image-policy.json. Drifting silently is"
              "\n    the failure the pin exists to prevent.")
    print(f"\n  {len(notable)} of {len(items)} release(s) carry a decision-moving signal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
