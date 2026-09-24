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
# than hardcoded: the ai-control-plane -> ai-control-plane rename broke a hardcoded
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

    # NAMED TAGS — written after a digest pin silently became a different image.
    # A ref pinned as `image@sha256:...` with no tag gives Dependabot nothing to
    # track, so its docker PRs propose the digest of `:latest`. On 2026-09-03 such a
    # bump was accepted: the policy said node:22-bookworm-slim and
    # python:3.12-alpine, the digests were node:latest (Node 26, full Debian 13) and
    # python:latest (3.14, full Debian 13), and every check here passed — the
    # policy digest WAS in use, and the comment above the FROM line still said the
    # right tag. The security scan went red ten days later on CVEs in packages a
    # slim image never contains. Pinning `image:tag@sha256:...` makes Dependabot
    # bump the digest WITHIN the tag, and makes a tag change a visible diff.
    #
    # What this can and cannot prove, statically: that every ref names the tag the
    # policy records and uses the digest the policy records. It CANNOT prove the
    # digest belongs to that tag — that needs the registry. Resolve it at re-pin
    # time (`docker buildx imagetools inspect <image:tag>`), and read what the image
    # actually is before accepting any bump.
    print("\nEvery pinned ref names its policy tag (digest-only refs make Dependabot follow :latest):")
    by_ref = {i["ref"]: i for i in policy.get("images", [])}
    for r in refs:
        ref = r["ref"]
        if ref.startswith(LOCAL_PREFIXES) or "@sha256:" not in ref:
            continue
        name_tag, digest = ref.split("@", 1)
        # The tag is after the last ':' of the final path segment, so a registry
        # port (host:5000/img) is not mistaken for one.
        head, _, last = name_tag.rpartition("/")
        if ":" in last:
            base, tag = last.rsplit(":", 1)
            name = f"{head}/{base}" if head else base
        else:
            name, tag = name_tag, None
        img = by_ref.get(name)
        want = (img or {}).get("tag")
        if not img or not want or want == "latest":
            continue
        where = f"{r['file']}:{r['line']}"
        check(f"{where} {name} names tag {want!r}", tag == want,
              (f"no tag — Dependabot will propose `{name}:latest` digests; write {name}:{want}@<digest>"
               if tag is None else
               f"tag {tag!r} does not match the policy's {want!r} — a tag change is a deliberate re-pin: "
               f"update context/image-policy.json (and verify what the new digest is)"))
        check(f"{where} {name} uses the digest the policy records", digest == img.get("digest"),
              f"{digest[:19]}... vs policy {str(img.get('digest'))[:19]}...")

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
