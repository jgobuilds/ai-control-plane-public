#!/usr/bin/env python3
r"""Image pinning gate — every third-party image is digest-pinned, and the pins
are reviewed on a schedule.

Two failure modes this prevents, and they pull in opposite directions:

  1. FLOATING TAGS. `image: foo:latest` means a `docker compose up -d` on any day
     silently changes what you run, and you cannot say what is running without
     looking. Non-reproducible, and it hides a downgrade as easily as an upgrade.
  2. STALE PINS. Pinning WITHOUT an update process is *worse* than :latest —
     you freeze, deliberately, on a known-vulnerable image and nothing tells you.

So this checks both: everything is pinned, AND the pins have been reviewed
recently. Same forcing-function shape as model_policy_check.py — a dated gate
that FAILS when the date passes, rather than a dashboard nobody opens.

Static only — parses files, never calls docker — so it runs in CI where there is
no daemon. The complementary runtime check (are the pinned images actually free
of known CVEs?) is the scheduled Trivy scan in .github/workflows/security-scan.yml.

    python scripts/image_policy_check.py

Pure stdlib. Exit 1 on any failure.
"""
import os, re, sys, json, glob, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY = os.path.join(ROOT, "context", "image-policy.json")

# Images built from this repo — reproducible via their pinned FROM + committed
# source, so a local tag is fine. DERIVED from the Compose project name rather
# than hardcoded: the brightworks -> ai-control-plane rename broke a hardcoded
# prefix here, and a check that fails on a rename teaches people to weaken it.
def _local_prefixes():
    for cf in ("docker-compose.yml", "compose.yml"):
        path = os.path.join(ROOT, cf)
        if os.path.isfile(path):
            for line in open(path, encoding="utf-8"):
                m = re.match(r"^name:\s*(\S+)", line)
                if m:
                    return (m.group(1) + "-",)
    return ()


LOCAL_PREFIXES = _local_prefixes()

fails, notes = [], []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        fails.append(f"{label}{(' — ' + detail) if detail else ''}")


def scan_files():
    """Every image reference in compose files and Dockerfiles."""
    refs = []
    targets = (glob.glob(os.path.join(ROOT, "*compose*.yml"))
               + glob.glob(os.path.join(ROOT, "*", "Dockerfile*")))
    for path in sorted(targets):
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            s = line.split("#", 1)[0].strip()      # ignore trailing comments
            m = re.match(r"(?:image:\s*|FROM\s+)(\S+)", s)
            if m:
                refs.append({"file": rel, "line": i, "ref": m.group(1)})
    return refs


def main():
    if not os.path.exists(POLICY):
        print(f"FAILED: no image policy at {os.path.relpath(POLICY, ROOT)}", file=sys.stderr)
        return 1
    policy = json.load(open(POLICY, encoding="utf-8"))

    print("Image pins (every third-party image is digest-pinned):")
    refs = scan_files()
    if not refs:
        check("found image references to check", False, "no compose/Dockerfile refs parsed")
    for r in refs:
        if r["ref"].startswith(LOCAL_PREFIXES):
            continue
        check(f"{r['file']}:{r['line']} pinned by digest",
              "@sha256:" in r["ref"], f"floating ref {r['ref']!r} — pin it to a digest")

    # The policy file and the actual files must agree — otherwise someone bumped
    # an image without recording why, and the risk notes go stale silently.
    # Searched by digest across compose, Dockerfiles AND workflows, because a
    # pinned image is not always an `image:`/`FROM` line — the CI scanner is
    # pinned inside a workflow step.
    print("\nPolicy record matches what the files actually use:")
    haystack = ""
    for path in (glob.glob(os.path.join(ROOT, "*compose*.yml"))
                 + glob.glob(os.path.join(ROOT, "*", "Dockerfile*"))
                 + glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))):
        haystack += open(path, encoding="utf-8").read()
    in_files = set(re.findall(r"sha256:[0-9a-f]{64}", haystack))
    for img in policy.get("images", []):
        d = img.get("digest", "")
        check(f"{img['ref']} ({img.get('tag','?')}) digest recorded in policy is in use",
              d in in_files, "policy digest not found in any compose/Dockerfile — "
                             "update context/image-policy.json when you re-pin")

    # Regression guard, written after breaking it: a trailing `# comment` on a
    # FROM line is NOT a comment to Docker's parser — it is parsed as extra
    # arguments and the build dies with "FROM requires either one or three
    # arguments". Adding the pins introduced exactly this in all six Dockerfiles
    # and nothing caught it, because no CI job built an image. This is the cheap
    # static half of that lesson; the daily built-image scan is the other half.
    print("\nDockerfile FROM lines parse (no trailing '#', which Docker reads as arguments):")
    for path in sorted(glob.glob(os.path.join(ROOT, "*", "Dockerfile*"))):
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        bad = [i for i, l in enumerate(open(path, encoding="utf-8"), 1)
               if re.match(r"^FROM\s+\S+\s+#", l)]
        check(f"{rel} FROM has no trailing comment", not bad,
              f"line(s) {bad} — move the comment to its own line ABOVE the FROM")

    print("\nPin freshness (a pin with no review process is a frozen vulnerability):")
    raw = policy.get("reviewBy")
    try:
        due = datetime.date.fromisoformat(raw)
    except (TypeError, ValueError):
        check("reviewBy is a valid ISO date", False, f"got {raw!r}")
        due = None
    if due:
        today = datetime.date.today()
        left = (due - today).days
        check(f"image pins reviewed by {due} ({left} day(s) left)", left >= 0,
              f"review is {abs(left)} day(s) overdue — re-resolve digests, read the "
              f"changelogs, re-run the security scan, then move reviewBy")
        if 0 <= left <= 7:
            notes.append(f"review due in {left} day(s) — schedule it")

    print()
    for n in notes:
        print(f"  NOTE  {n}")

    # Drift we KNOW about and chose to defer. Printing it here — inside the check
    # that gates the pins — means the queue is read at the moment someone reviews
    # the policy, instead of living in a note nobody opens. A deferral is a
    # decision; an undated intention is how a pin quietly becomes stale.
    pending = policy.get("pendingBumps") or []
    if pending:
        print(f"\n  QUEUED FOR THE NEXT REVIEW ({len(pending)}):")
        for b in pending:
            print(f"    {b.get('ref')}: {b.get('from')} -> {b.get('to')}"
                  f"  (found {b.get('noticedOn')} by {b.get('noticedBy')})")
            print(f"      due {b.get('scheduledFor')} — {b.get('action', '')}")
    if fails:
        print(f"\nFAILED: {len(fails)} check(s)")
        for f in fails:
            print("  - " + f)
        print("\nSee ai-standards/references/dependency-security.md")
        return 1
    print(f"All image pins current ({len(policy.get('images', []))} pinned, "
          f"review by {policy.get('reviewBy')}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
