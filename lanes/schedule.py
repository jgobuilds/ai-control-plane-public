#!/usr/bin/env python3
"""Schedule the lanes without n8n — stdlib only ([ADR 0016](../docs/decisions/0016-replace-n8n.md)).

WHY NOT APScheduler. `lanes/server.py` is pure stdlib and says so, as does every
shared script in this repo. Eight fixed schedules do not justify the first
runtime dependency in a service that has none — `build-vs-adopt` cuts both ways,
and a cron table is not the part of an orchestrator that is hard.

WHAT IS DELIBERATELY DIFFERENT FROM n8n
---------------------------------------
n8n does not backfill a missed schedule and does not tell you it missed one. The
gap-closure plan names the consequence: *"a gap in a metric means the laptop
slept, which is indistinguishable from the lane broke unless something records
the difference."* So this records the difference. A fire that was due while the
process was down is emitted as a **MISSED** event with the due time — not
silently skipped, and not replayed either, because replaying a 06:00 retention
sweep at 14:00 is a different action from running it at 06:00.

That is the one behaviour here that is better than what it replaces, and it
exists because the failure was written down before the code was.

MODES
-----
`dry` (default) computes and logs, and **calls nothing**. That is what makes it
safe to run beside n8n during cutover: two schedulers firing the same lane would
double-run every lane, which for the retention sweep means running a destructive
job twice. Cutover flips one lane at a time to `live` via LIVE_LANES.

    AICP_SCHED_MODE=dry|live     default dry
    AICP_SCHED_LIVE_LANES=a,b    per-lane opt-in, even in live mode
    AICP_SCHED_TZ=America/New_York
"""
from __future__ import annotations
import json, os, threading, time, urllib.request
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9
    ZoneInfo = None

# Mirrors the n8n scheduleTrigger nodes EXACTLY as read from the workflow JSON
# (eight on 2026-07-28; seam-heartbeat added 2026-08-03). Divergence here is a silent behaviour change during
# cutover, so `tests/schedule_test.py` asserts this table against that JSON
# rather than trusting it stays in step.
#
# kind: "every"  -> minutes interval
#       "daily"  -> at hour:minute
#       "weekly" -> on weekday (Mon=0) at hour:minute
SCHEDULES = {
    "dispatch":         {"kind": "every",  "minutes": 10},
    "deliverables":     {"kind": "every",  "minutes": 15},
    "drive-sync":       {"kind": "every",  "minutes": 15},
    "retention-sweep":  {"kind": "daily",  "hour": 6,  "minute": 0},
    "daily-digest":     {"kind": "daily",  "hour": 7,  "minute": 30},
    "dependency-bumps": {"kind": "weekly", "weekday": 0, "hour": 6, "minute": 0},
    "eval-drift":       {"kind": "weekly", "weekday": 0, "hour": 7, "minute": 0},
    "research-watch":   {"kind": "weekly", "weekday": 0, "hour": 7, "minute": 30},
    "recurrence-review":{"kind": "weekly", "weekday": 0, "hour": 8, "minute": 0},
    # Liveness probe for the ask-human webhook. No ENDPOINTS entry: it is not a
    # lane this service runs, it is an n8n-internal HTTP knock, so at cutover it
    # stays in n8n rather than moving here. The table still has to list it —
    # the check is bidirectional on purpose, so an n8n schedule this file does
    # not know about is a failure whether or not we intend to own it.
    "seam-heartbeat":   {"kind": "every",  "minutes": 15},
}

# lane name -> the endpoint on this service that n8n currently calls
ENDPOINTS = {
    "dispatch": "/dispatch", "retention-sweep": "/retention",
    "daily-digest": "/digest", "eval-drift": "/eval-metrics",
    "research-watch": "/research", "recurrence-review": "/recurrence",
}

STATE = os.environ.get("AICP_SCHED_STATE", "/audit/scheduler-state.json")


def tz():
    name = os.environ.get("AICP_SCHED_TZ", "America/New_York")
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        # Windows without tzdata. Refuse rather than silently running in UTC —
        # a 06:00 sweep four hours early is a real behaviour change.
        raise RuntimeError(
            f"timezone {name!r} unavailable; install tzdata or set AICP_SCHED_TZ")


def next_fire(spec, after):
    """First firing STRICTLY after `after`. Pure; `after` is tz-aware."""
    k = spec["kind"]
    if k == "every":
        m = spec["minutes"]
        # Align to the wall clock, not to process start: an every-10 lane should
        # fire at :00 :10 :20, so a restart does not shift the whole series.
        base = after.replace(second=0, microsecond=0)
        mins = base.hour * 60 + base.minute
        nxt = ((mins // m) + 1) * m
        return base.replace(hour=0, minute=0) + timedelta(minutes=nxt)
    if k == "daily":
        cand = after.replace(hour=spec["hour"], minute=spec["minute"],
                             second=0, microsecond=0)
        return cand if cand > after else cand + timedelta(days=1)
    if k == "weekly":
        cand = after.replace(hour=spec["hour"], minute=spec["minute"],
                             second=0, microsecond=0)
        delta = (spec["weekday"] - cand.weekday()) % 7
        cand += timedelta(days=delta)
        return cand if cand > after else cand + timedelta(days=7)
    raise ValueError(f"unknown schedule kind {k!r}")


def load_state(path=None):
    p = path or STATE
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state, path=None):
    p = path or STATE
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.replace(tmp, p)          # atomic; a torn state file reads as empty
    except OSError:
        pass                        # scheduling must not die on a full disk


def missed_since(lane, spec, last_iso, now):
    """Firings that were due while nobody was listening.

    Returned, not replayed. Running a 06:00 retention sweep at 14:00 is a
    different action from running it at 06:00, and the caller decides.
    """
    if not last_iso:
        return []
    try:
        t = datetime.fromisoformat(last_iso)
    except ValueError:
        return []
    out = []
    while len(out) < 500:           # bound: a month offline must not hang boot
        t = next_fire(spec, t)
        if t >= now:
            break
        out.append(t)
    return out


def live_for(lane):
    """Per-lane opt-in. Cutover is one lane at a time (ADR 0016)."""
    if os.environ.get("AICP_SCHED_MODE", "dry").lower() != "live":
        return False
    allow = os.environ.get("AICP_SCHED_LIVE_LANES", "").strip()
    if not allow or allow == "*":
        return True
    return lane in {x.strip() for x in allow.split(",") if x.strip()}


def fire(lane, base_url, emit):
    path = ENDPOINTS.get(lane)
    if not path:
        emit(f"SKIP    {lane}: no endpoint mapped")
        return False
    if not live_for(lane):
        emit(f"DRY     {lane}: would POST {path}")
        return False
    try:
        req = urllib.request.Request(base_url.rstrip("/") + path, method="POST")
        with urllib.request.urlopen(req, timeout=600) as r:
            emit(f"FIRED   {lane}: {r.status}")
            return True
    except Exception as e:                      # noqa: BLE001 - never die
        emit(f"ERROR   {lane}: {e.__class__.__name__}: {e}")
        return False


def run(emit=print, base_url=None, once=False, _sleep=time.sleep):
    base = base_url or os.environ.get("AICP_LANES_URL", "http://127.0.0.1:8099")
    zone = tz()
    state = load_state()
    now = datetime.now(zone)

    for lane, spec in sorted(SCHEDULES.items()):
        for m in missed_since(lane, spec, state.get(lane, {}).get("last"), now):
            emit(f"MISSED  {lane}: due {m.isoformat()} while the scheduler was down")

    emit(f"scheduler up · tz={zone} · mode={os.environ.get('AICP_SCHED_MODE','dry')}")
    while True:
        now = datetime.now(zone)
        due = {l: next_fire(s, now) for l, s in SCHEDULES.items()}
        lane, when = min(due.items(), key=lambda kv: kv[1])
        wait = max(0.0, (when - now).total_seconds())
        emit(f"next    {lane} at {when.isoformat()} (in {int(wait)}s)")
        if once:
            return lane, when
        _sleep(min(wait, 300))                  # wake often; the clock can jump
        now = datetime.now(zone)
        for l, w in due.items():
            if w <= now:
                fire(l, base, emit)
                state.setdefault(l, {})["last"] = w.isoformat()
        save_state(state)


def start_background(emit=print):
    t = threading.Thread(target=run, kwargs={"emit": emit}, daemon=True,
                         name="lane-scheduler")
    t.start()
    return t


if __name__ == "__main__":
    run()
