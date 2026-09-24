#!/usr/bin/env python3
"""Prove the staleness ceiling decides correctly, offline, from synthetic release timelines.

pin_staleness.py reads the npm registry, so its CI signal is a weekly scheduled run —
which is exactly the kind of check that can pass for the wrong reason (nothing
eligible yet, a prerelease miscounted, a date off by the cooldown). assess() is pure,
so every branch is pinned here with dates chosen to sit on either side of a boundary:

    no newer release                         -> current
    newer release still inside cooldown      -> current (and says when it clears)
    eligible 10 days ago                     -> behind, 20 days left
    eligible exactly 30 days ago             -> behind (the ceiling is inclusive)
    eligible 31 days ago                     -> stale
    a prerelease newer than the pin          -> ignored
    pinned version not published / not semver -> unknown (a failure, not a pass)
    many releases, daily cadence             -> measured from the FIRST eligible one,
                                                recommending the NEWEST eligible one

    python tests/pin_staleness_test.py
"""
import datetime as dt, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import pin_staleness as ps  # noqa: E402

NOW = dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.timezone.utc)
fails = []


def at(days_ago):
    return (NOW - dt.timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def expect(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def main():
    print("The staleness ceiling (cooldown 7d floor, 30d ceiling):")

    r = ps.assess("1.0.0", {"created": at(400), "1.0.0": at(100)}, NOW)
    expect("no newer release is current", r["status"] == "current", r)

    r = ps.assess("1.0.0", {"1.0.0": at(100), "1.0.1": at(3)}, NOW)
    expect("a newer release inside its cooldown is still current", r["status"] == "current", r)
    expect("...and says when it clears", "clears cooldown" in r["detail"], r)

    # eligible 10 days ago = published 17 days ago
    r = ps.assess("1.0.0", {"1.0.0": at(100), "1.1.0": at(17)}, NOW)
    expect("eligible 10d ago is behind", r["status"] == "behind" and r["waited_days"] == 10, r)
    expect("...with 20 days left", "20d left" in r["detail"], r)

    r = ps.assess("1.0.0", {"1.0.0": at(100), "1.1.0": at(37)}, NOW)
    expect("eligible exactly 30d ago is still behind (inclusive ceiling)",
           r["status"] == "behind" and r["waited_days"] == 30, r)

    r = ps.assess("1.0.0", {"1.0.0": at(100), "1.1.0": at(38)}, NOW)
    expect("eligible 31d ago is stale", r["status"] == "stale" and r["waited_days"] == 31, r)

    r = ps.assess("1.0.0", {"1.0.0": at(100), "2.0.0-beta.1": at(90)}, NOW)
    expect("a prerelease is not an eligible upgrade", r["status"] == "current", r)

    r = ps.assess("9.9.9", {"1.0.0": at(100)}, NOW)
    expect("an unpublished pin is unknown, not current", r["status"] == "unknown", r)
    r = ps.assess("1.0.0-rc.1", {"1.0.0-rc.1": at(100)}, NOW)
    expect("a non-stable pin is unknown", r["status"] == "unknown", r)

    # Daily releases 60..1 days ago on top of a pin published 61 days ago.
    daily = {"1.0.0": at(61)}
    for i, d in enumerate(range(60, 0, -1), start=1):
        daily[f"1.0.{i}"] = at(d)
    r = ps.assess("1.0.0", daily, NOW)
    # first newer (1.0.1) published 60d ago -> eligible 53d ago; newest cleared was published 7d ago
    expect("daily cadence is measured from the FIRST eligible release",
           r["status"] == "stale" and r["waited_days"] == 53, r)
    expect("...and recommends the NEWEST release that has cleared cooldown",
           r["move_to"] == "1.0.54", r)

    r = ps.assess("1.0.0", {"1.0.0": at(20), "1.0.10": at(15), "1.0.9": at(16)}, NOW)
    expect("versions compare numerically, not as strings (1.0.10 > 1.0.9)",
           r["move_to"] == "1.0.10", r)

    # The floor is stated in two places: this script's COOLDOWN_DAYS and the minimum
    # dependabot_check.py enforces on every Dependabot entry. If they drift, the
    # ceiling measures from a different day than the one Dependabot waits for.
    import dependabot_check as dc
    expect("COOLDOWN_DAYS matches dependabot_check.MIN_COOLDOWN_DAYS",
           ps.COOLDOWN_DAYS == dc.MIN_COOLDOWN_DAYS, f"{ps.COOLDOWN_DAYS} vs {dc.MIN_COOLDOWN_DAYS}")

    print()
    if fails:
        print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
        return 1
    print("All pin-staleness checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
