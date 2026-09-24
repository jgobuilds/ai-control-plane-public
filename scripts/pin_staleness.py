#!/usr/bin/env python3
"""Pin staleness ceiling: fail when an eligible upgrade has been left untaken too long.

WHY THIS EXISTS. Pinning has two failure modes that pull in opposite directions:

  1. MOVING TOO EARLY — taking a release before anyone else has run it. That is
     what the 7-day cooldown in .github/dependabot.yml and the pin rule in
     docs/runbooks/UPGRADES.md guard against.
  2. FALLING TOO FAR BEHIND — a pin that nobody moves. Every week behind makes the
     eventual jump bigger, and when a security fix finally forces it, the jump
     lands on a stale base all at once, under pressure. "Pinning without an update
     process is worse than :latest" (ai-standards dependency-security lens).

The cooldown is a FLOOR. This is the CEILING.

WHAT IT MEASURES. Not "how old is the pin" — a project that releases daily would
make every pin look stale within a week. Instead: for each pin, find the first
newer stable release, add the cooldown to get the day it became ELIGIBLE, and ask
how long ago that was. That is the time an upgrade has been available, cleared
by policy, and not taken. Past CEILING_DAYS it fails.

    python scripts/pin_staleness.py             # check every pin, exit 1 if any is stale
    python scripts/pin_staleness.py --json      # machine-readable

SCOPE. npm pins only: the runners' <runner>/tools/package.json (exact versions,
locked) and the builder's CLAUDE_CODE_VERSION build arg. Base images are held by a
different, stricter control — context/image-policy.json's dated reviewBy, enforced
by image_policy_check.py. The builder's cli-printing-press commit pin is not
checked here (GitHub releases, not npm); it carries its own REVISIT note.

NETWORK. It reads the public npm registry, so it is NOT a deterministic CI gate:
it runs weekly in .github/workflows/pin-staleness.yml and on demand. When the
registry cannot be read it exits 2 — a check that could not run is not a pass.
The decision logic (assess) takes data, not network, and is covered offline by
tests/pin_staleness_test.py.

Stdlib only.
"""
from __future__ import annotations
import argparse, datetime as dt, glob, json, os, re, shutil, subprocess, sys, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The floor: must match cooldown.default-days in .github/dependabot.yml, which
# dependabot_check.py enforces as MIN_COOLDOWN_DAYS.
COOLDOWN_DAYS = 7
# The ceiling: once an upgrade is eligible, it may wait this long for a human to
# take it. 30 days covers a monthly review cycle with room for one missed week,
# and matches the image-policy reviewBy cadence, so both kinds of pin go stale on
# the same clock.
CEILING_DAYS = 30

STABLE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def semver(v):
    m = STABLE.match(v)
    return tuple(int(x) for x in m.groups()) if m else None


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def assess(pin, times, now, cooldown=COOLDOWN_DAYS, ceiling=CEILING_DAYS):
    """Decide one pin from registry publish times. Pure: no network, no clock.

    `times` is the registry's `time` map ({version: ISO timestamp, ...}; extra
    keys such as "created"/"modified" are ignored). Returns a dict with `status`:
      current  no newer stable release has cleared the cooldown yet
      behind   an eligible upgrade exists, still inside the ceiling
      stale    an eligible upgrade has waited longer than the ceiling  -> FAIL
      unknown  the pinned version is not a published stable release  -> FAIL
    """
    pv = semver(pin)
    if pv is None or pin not in times:
        return {"status": "unknown", "detail": f"{pin} is not a published stable release"}
    stable = {v: parse_time(t) for v, t in times.items() if semver(v)}
    newer = sorted((v for v in stable if semver(v) > pv), key=semver)
    eligible = {v: stable[v] + dt.timedelta(days=cooldown) for v in newer}
    cleared = [v for v in newer if eligible[v] <= now]
    if not cleared:
        pending = min(newer, key=lambda v: eligible[v]) if newer else None
        return {"status": "current",
                "detail": (f"{pending} clears cooldown on {eligible[pending].date()}" if pending
                           else "no newer stable release")}
    first = min(cleared, key=lambda v: eligible[v])
    waited = (now - eligible[first]).days
    target = max(cleared, key=semver)       # the newest release policy allows today
    base = {"eligible_since": eligible[first].date().isoformat(), "waited_days": waited,
            "move_to": target}
    if waited > ceiling:
        return {**base, "status": "stale",
                "detail": f"{first} became eligible {waited}d ago (ceiling {ceiling}d); move to {target}"}
    return {**base, "status": "behind",
            "detail": f"{first} eligible {waited}d ago, {ceiling - waited}d left; policy allows {target}"}


def discover_pins():
    """Every npm pin this repo owns: (source, package, version)."""
    pins = []
    for path in sorted(glob.glob(os.path.join(ROOT, "*", "tools", "package.json"))):
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        deps = json.load(open(path, encoding="utf-8")).get("dependencies") or {}
        for name, ver in deps.items():
            pins.append((rel, name, ver))
    builder = os.path.join(ROOT, "capability-factory", "Dockerfile.builder")
    if os.path.isfile(builder):
        m = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)", open(builder, encoding="utf-8").read(), re.M)
        if m:
            pins.append(("capability-factory/Dockerfile.builder", "@anthropic-ai/claude-code", m.group(1)))
    return pins


def registry_times(package):
    url = "https://registry.npmjs.org/" + urllib.parse.quote(package, safe="@")
    # curl first: this workstation's Python CA bundle rejects https (see
    # scripts/fetch_source.py); CI's does not. Never fall back to unverified TLS.
    if shutil.which("curl"):
        r = subprocess.run(["curl", "-fsSL", "-m", "30", "-H", "Accept: application/json", url],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            return json.loads(r.stdout)["time"]
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                timeout=30) as resp:
        return json.load(resp)["time"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    pins = discover_pins()
    if not pins:
        print("FAIL found no npm pins to check — a staleness check over nothing is not a pass")
        return 2
    results, unreachable = [], []
    cache = {}
    for source, pkg, ver in pins:
        if pkg not in cache:
            try:
                cache[pkg] = registry_times(pkg)
            except Exception as e:                       # network, JSON, HTTP
                cache[pkg] = None
                unreachable.append(f"{pkg}: {type(e).__name__}")
        if cache[pkg] is None:
            results.append({"source": source, "package": pkg, "pin": ver, "status": "unreachable"})
            continue
        results.append({"source": source, "package": pkg, "pin": ver, **assess(ver, cache[pkg], now)})

    if a.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"Pin staleness — cooldown {COOLDOWN_DAYS}d (floor), ceiling {CEILING_DAYS}d, "
              f"as of {now.date()}:")
        for r in results:
            mark = {"current": "PASS", "behind": "NOTE", "stale": "FAIL",
                    "unknown": "FAIL", "unreachable": "????"}[r["status"]]
            ref = f"{r['package']}@{r['pin']}"
            print(f"  {mark}  {ref:<36} {r['status']:<8} {r.get('detail', '')}   [{r['source']}]")
    bad = [r for r in results if r["status"] in ("stale", "unknown")]
    if bad:
        print(f"\nFAILED: {len(bad)} pin(s) past the ceiling or unpublished. Move them following "
              "docs/runbooks/UPGRADES.md —\n  cooldown-cleared version, lockfile regenerated, "
              "enforcement probe, canary.")
        return 1
    if unreachable:
        print("\nCOULD NOT CHECK: " + "; ".join(unreachable) + " — not a pass.")
        return 2
    print("\nNo pin is past the ceiling.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
