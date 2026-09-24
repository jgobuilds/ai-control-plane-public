#!/usr/bin/env python3
"""Corpus for the lane scheduler (ADR 0016).

The property that matters most is not "does it compute a time" — it is **does it
compute the SAME time n8n does**. A cutover that silently shifts the retention
sweep by an hour is worse than not cutting over, so the schedule table is
asserted against the workflow JSON rather than against itself.

    python tests/schedule_test.py
"""
import importlib.util, json, glob, os, sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
spec = importlib.util.spec_from_file_location(
    "sched", os.path.join(ROOT, "lanes", "schedule.py"))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


UTC = timezone.utc
def dt(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


print("The table matches the n8n workflows it replaces (the cutover property):")
seen = {}
for p in glob.glob(os.path.join(ROOT, "n8n-workflows", "*.json")):
    d = json.load(open(p, encoding="utf-8"))
    lane = str(d.get("id", "")).replace("aicp-", "")
    for n in d.get("nodes", []):
        if n.get("type", "").split(".")[-1] != "scheduleTrigger":
            continue
        iv = (n.get("parameters", {}).get("rule", {}).get("interval") or [{}])[0]
        seen[lane] = iv

check("every n8n schedule has a table entry",
      set(seen) <= set(sc.SCHEDULES), f"missing: {sorted(set(seen) - set(sc.SCHEDULES))}")
check("no table entry invents a schedule n8n does not have",
      set(sc.SCHEDULES) <= set(seen), f"extra: {sorted(set(sc.SCHEDULES) - set(seen))}")

for lane, iv in sorted(seen.items()):
    ours = sc.SCHEDULES.get(lane)
    if not ours:
        continue
    f = iv.get("field")
    if f == "minutes":
        check(f"{lane}: interval matches n8n",
              ours["kind"] == "every" and ours["minutes"] == iv.get("minutesInterval"),
              f"n8n={iv} ours={ours}")
    elif f == "days":
        check(f"{lane}: daily time matches n8n",
              ours["kind"] == "daily" and ours["hour"] == iv.get("triggerAtHour", 0)
              and ours["minute"] == iv.get("triggerAtMinute", 0), f"n8n={iv} ours={ours}")
    elif f == "weeks":
        days = iv.get("triggerAtDay", [0])
        check(f"{lane}: weekly slot matches n8n",
              ours["kind"] == "weekly" and ours["hour"] == iv.get("triggerAtHour", 0)
              and ours["minute"] == iv.get("triggerAtMinute", 0)
              and ours["weekday"] == (days[0] - 1) % 7, f"n8n={iv} ours={ours}")

print("\nnext_fire is strictly after 'now' — never returns the current instant:")
# A boundary bug here double-fires: fire at 06:00, store 06:00, recompute, get
# 06:00 again. That is the classic scheduler defect and it is silent.
now = dt(2026, 7, 28, 6, 0)
check("daily at the exact due minute rolls to tomorrow",
      sc.next_fire(sc.SCHEDULES["retention-sweep"], now) == dt(2026, 7, 29, 6, 0),
      str(sc.next_fire(sc.SCHEDULES["retention-sweep"], now)))
check("interval at an exact boundary advances",
      sc.next_fire({"kind": "every", "minutes": 10}, dt(2026, 7, 28, 6, 10))
      == dt(2026, 7, 28, 6, 20))

print("\nIntervals align to the wall clock, not to process start:")
# Else a restart at 06:03 shifts an every-10 lane to :03 :13 :23 forever.
check("every-10 from 06:03 gives 06:10",
      sc.next_fire({"kind": "every", "minutes": 10}, dt(2026, 7, 28, 6, 3))
      == dt(2026, 7, 28, 6, 10))
check("every-15 from 06:01 gives 06:15",
      sc.next_fire({"kind": "every", "minutes": 15}, dt(2026, 7, 28, 6, 1))
      == dt(2026, 7, 28, 6, 15))
check("every-10 crosses midnight",
      sc.next_fire({"kind": "every", "minutes": 10}, dt(2026, 7, 28, 23, 55))
      == dt(2026, 7, 29, 0, 0))

print("\nWeekly lands on the right weekday:")
# 2026-07-28 is a Tuesday; Monday-scheduled lanes must wait six days.
check("Mon 07:00 from Tue is next Monday",
      sc.next_fire(sc.SCHEDULES["eval-drift"], dt(2026, 7, 28, 9, 0))
      == dt(2026, 8, 3, 7, 0), str(sc.next_fire(sc.SCHEDULES["eval-drift"], dt(2026, 7, 28, 9, 0))))
check("Mon 07:00 from Monday 06:00 is the same day",
      sc.next_fire(sc.SCHEDULES["eval-drift"], dt(2026, 8, 3, 6, 0)) == dt(2026, 8, 3, 7, 0))

print("\nMissed firings are REPORTED, not replayed and not swallowed:")
missed = sc.missed_since("retention-sweep", sc.SCHEDULES["retention-sweep"],
                         dt(2026, 7, 25, 6, 0).isoformat(), dt(2026, 7, 28, 9, 0))
check("three days offline reports three missed sweeps", len(missed) == 3, str(missed))
check("no state means no missed report (not a flood on first boot)",
      sc.missed_since("x", sc.SCHEDULES["retention-sweep"], None, dt(2026, 7, 28)) == [])
check("a corrupt timestamp does not raise",
      sc.missed_since("x", sc.SCHEDULES["retention-sweep"], "not-a-date", dt(2026, 7, 28)) == [])
long = sc.missed_since("x", sc.SCHEDULES["dispatch"],
                       dt(2020, 1, 1).isoformat(), dt(2026, 7, 28))
check("a years-old state file is bounded, not an infinite loop", len(long) <= 500, str(len(long)))

print("\nDry mode calls NOTHING — the property that makes it safe beside n8n:")
os.environ.pop("AICP_SCHED_MODE", None)
os.environ.pop("AICP_SCHED_LIVE_LANES", None)
msgs = []
fired = sc.fire("retention-sweep", "http://127.0.0.1:9", msgs.append)
check("dry mode does not fire", fired is False)
check("and says what it would have done", any("DRY" in m for m in msgs), str(msgs))

print("\nLive mode is per-lane, so cutover is one lane at a time:")
os.environ["AICP_SCHED_MODE"] = "live"
os.environ["AICP_SCHED_LIVE_LANES"] = "dispatch"
check("an opted-in lane is live", sc.live_for("dispatch") is True)
check("a lane NOT opted in stays dry", sc.live_for("retention-sweep") is False)
os.environ["AICP_SCHED_LIVE_LANES"] = ""
check("live with an empty allowlist means all lanes", sc.live_for("retention-sweep") is True)
os.environ["AICP_SCHED_MODE"] = "dry"
check("dry mode overrides an allowlist", sc.live_for("dispatch") is False)

print("\nA live fire against a dead endpoint is reported, never raised:")
os.environ["AICP_SCHED_MODE"] = "live"
os.environ["AICP_SCHED_LIVE_LANES"] = "*"
msgs = []
check("returns False instead of crashing the scheduler thread",
      sc.fire("dispatch", "http://127.0.0.1:9", msgs.append) is False)
check("and the reason is in the log", any("ERROR" in m for m in msgs), str(msgs))
os.environ["AICP_SCHED_MODE"] = "dry"

print("\nAn unmapped lane is skipped loudly, not silently:")
msgs = []
sc.fire("dependency-bumps", "http://x", msgs.append)
check("unmapped lane says so", any("no endpoint mapped" in m for m in msgs), str(msgs))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All scheduler checks passed.")
