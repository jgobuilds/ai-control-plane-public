#!/usr/bin/env python3
"""Collect the facts a status page shows. Pure data — no rendering, no opinions.

Split from the renderer so it is testable without HTML. Every field here comes
from something that already exists; this file invents no new source of truth:

    containers   docker ps
    gates        the existing check scripts, by exit code
    ledger       audit/decisions.jsonl (the router writes it; hash-chained)
    lanes        lanes/schedule.py SCHEDULES vs the scheduler state file
    sessions     Claude Code transcripts in the claude-config volume

THE SESSION HALF REPLACES A THIRD-PARTY CONTAINER. `agentsview` mounted that same
volume and showed usage analytics — sessions, messages, projects, active days,
an activity heatmap. It had been reading 0 of 71 session files for weeks without
anyone noticing, which is a fair argument for owning the ~80 lines rather than
running a container for them.

Reading the volume needs a container because it is a named docker volume, not a
host path. Uses `docker run --rm` against an image already built here, so it
works whether or not any service is up.

Never raises for a missing source: a status page whose job is to report trouble
must not itself fail when something is broken. Each section carries its own
`error` instead.
"""
from __future__ import annotations
import datetime, json, os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT = os.environ.get("AICP_AUDIT_LOG", os.path.join(ROOT, "audit", "decisions.jsonl"))
SESSION_VOLUME = os.environ.get("AICP_SESSION_VOLUME", "ai-control-plane_claude-config")
SESSION_IMAGE = os.environ.get("AICP_SESSION_IMAGE", "ai-control-plane-lanes:latest")

# Gates worth a red light. Each is (label, argv, needs_docker). Exit code is the
# verdict — these scripts were written to be run this way, so the status page
# reuses them rather than reimplementing their logic and drifting from it.
GATES = [
    ("conformance", [sys.executable, "scripts/conformance_test.py"], False),
    ("scope privacy", [sys.executable, "tests/scope_privacy_test.py"], False),
    ("public hygiene", [sys.executable, "scripts/check_public_hygiene.py", "--tree"], False),
    ("workspace sync", [sys.executable, "scripts/sync_workspace.py", "--check"], False),
    ("audit chain", [sys.executable, "scripts/verify_audit.py"], False),
    ("mount drift", [sys.executable, "scripts/mount_drift_check.py", "--live"], True),
    ("n8n live state", [sys.executable, "scripts/n8n_doctor.py"], True),
    # The counting half of the recurrence register runs HERE, not in CI: the
    # occurrence log is gitignored runtime evidence, so this host is the only
    # place the count exists.
    ("recurrence", [sys.executable, "scripts/recurrence.py", "check"], False),
    ("router chokepoint", [sys.executable, "scripts/boundary_check.py"], False),
]


def _counted(values):
    """Frequency map, biggest first."""
    d = {}
    for v in values:
        d[v] = d.get(v, 0) + 1
    return dict(sorted(d.items(), key=lambda kv: -kv[1]))


def _run(argv, timeout=180, cwd=ROOT):
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"{type(e).__name__}: {e}"


def containers():
    code, out = _run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"], 30)
    if code != 0:
        return {"error": "docker unavailable", "items": []}
    items = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0]:
            items.append({"name": parts[0], "state": parts[1], "status": parts[2]})
    return {"items": sorted(items, key=lambda i: i["name"])}


def gates():
    out = []
    for label, argv, needs_docker in GATES:
        code, text = _run(argv)
        # A gate that cannot run is NOT a pass. Reporting "skipped" as green is
        # the vacuity this repo keeps finding; it gets its own state instead.
        state = "pass" if code == 0 else ("unknown" if code is None else "fail")
        if code == 0 and "SKIP" in text[:400]:
            state = "skipped"
        out.append({"name": label, "state": state,
                    "detail": text.strip().splitlines()[-1][:160] if text.strip() else ""})
    return {"items": out}


def block_category(raw):
    """Normalise a `blocked` value into something countable.

    The router writes clean values for policy blocks ("halted",
    "approval-required") and `error:<message>` for faults. Using the raw
    message as a category makes every one-off error its own bucket, so a
    dashboard shows fifteen categories of one instead of "runner error: 15".
    It also put a PROMPT on screen once — see finding D3. Module-level, not
    nested in ledger(), so tests/status_data_test.py can pin the mapping.
    """
    t = str(raw or "")
    if not t.startswith("error:"):
        return t or "unknown"
    m = t[6:].strip()
    for needle, label in (
        ("is retired", "retired scope"),
        ("unknown scope", "unknown scope"),
        ("ENOTFOUND", "runner unreachable"),
        ("blockCrossVendor", "cross-vendor blocked"),
        ("PII detected", "PII refused"),
        ("returned 5", "runner error"),
        ("no runner-map entry", "scope not mapped"),
        ("verify", "verify unavailable"),
    ):
        if needle in m:
            return label
    return "error: " + m.split(":")[0][:40]


def ledger(limit=200):
    if not os.path.isfile(AUDIT):
        return {"error": "no audit ledger yet", "records": 0}
    recs = []
    try:
        with open(AUDIT, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or " " not in line:
                    continue
                try:
                    recs.append(json.loads(line.split(" ", 1)[1]))
                except ValueError:
                    continue
    except OSError as e:
        return {"error": str(e), "records": 0}
    recent = recs[-limit:]

    def tally(key):
        d = {}
        for r in recent:
            v = r.get(key)
            if v is not None:
                d[str(v)] = d.get(str(v), 0) + 1
        return dict(sorted(d.items(), key=lambda kv: -kv[1]))
    blocked = [r for r in recent if r.get("blocked")]

    return {
        "records": len(recs),
        "window": len(recent),
        "first": recs[0].get("ts") if recs else None,
        "last": recs[-1].get("ts") if recs else None,
        "byScope": tally("scope"),
        "byTier": tally("tier"),
        "byModel": tally("model"),
        "blocked": len(blocked),
        "blockedReasons": _counted(block_category(r.get("blocked")) for r in blocked),
        "verifyStatus": tally("verifyStatus"),
        "escalations": sum(1 for r in recent if r.get("vendorEscalated")),
        "notionalUsd": round(sum((r.get("usage") or {}).get("notionalCostUsd") or 0
                                 for r in recent), 4),
    }


def lanes():
    sys.path.insert(0, os.path.join(ROOT, "lanes"))
    try:
        import schedule as sc  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        return {"error": f"cannot read the schedule table: {e}", "items": []}
    state = {}
    for cand in (os.environ.get("AICP_SCHED_STATE"),
                 os.path.join(ROOT, "audit", "scheduler-state.json")):
        if cand and os.path.isfile(cand):
            try:
                state = json.load(open(cand, encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            break
    items = []
    for lane, spec in sorted(sc.SCHEDULES.items()):
        last = (state.get("lanes") or state).get(lane) if isinstance(state, dict) else None
        if isinstance(last, dict):
            last = last.get("last") or last.get("lastRun")
        items.append({"lane": lane, "kind": spec.get("kind"), "spec": spec, "last": last})
    return {"items": items, "stateFound": bool(state)}


SESSION_SCRIPT = r"""
import json, os, sys, collections
root = "/sessions/claude/projects"
out = {"sessions": 0, "messages": 0, "projects": 0, "activeDays": 0,
       "byModel": {}, "byProject": {}, "days": {}, "outputTokens": 0}
if not os.path.isdir(root):
    print(json.dumps({"error": "no projects dir in the volume"})); sys.exit(0)
projects = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
out["projects"] = len(projects)
days = collections.Counter(); models = collections.Counter(); perproj = collections.Counter()
for p in projects:
    for fn in os.listdir(os.path.join(root, p)):
        if not fn.endswith(".jsonl"):
            continue
        out["sessions"] += 1
        perproj[p] += 1
        try:
            with open(os.path.join(root, p, fn), encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    out["messages"] += 1
                    ts = r.get("timestamp") or r.get("ts") or ""
                    if isinstance(ts, str) and len(ts) >= 10:
                        days[ts[:10]] += 1
                    msg = r.get("message") or {}
                    m = msg.get("model") if isinstance(msg, dict) else None
                    if m:
                        models[m] += 1
                    u = (msg.get("usage") if isinstance(msg, dict) else None) or {}
                    if isinstance(u, dict):
                        out["outputTokens"] += int(u.get("output_tokens") or 0)
        except OSError:
            continue
out["activeDays"] = len(days)
out["days"] = dict(sorted(days.items()))
out["byModel"] = dict(models.most_common(8))
out["byProject"] = dict(perproj.most_common(12))
print(json.dumps(out))
"""


def sessions():
    """Claude Code transcript analytics — the half agentsview used to show."""
    code, out = _run(["docker", "run", "--rm",
                      "-v", f"{SESSION_VOLUME}:/sessions/claude:ro",
                      "--entrypoint", "python", SESSION_IMAGE, "-c", SESSION_SCRIPT], 120)
    if code != 0:
        return {"error": f"could not read the session volume: {out.strip()[:200]}"}
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": f"unparseable session data: {out[:200]}"}


def collect(with_gates=True, with_sessions=True):
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "generated": now.isoformat(timespec="seconds"),
        "containers": containers(),
        "gates": gates() if with_gates else {"items": [], "skipped": True},
        "ledger": ledger(),
        "lanes": lanes(),
        "sessions": sessions() if with_sessions else {"skipped": True},
    }


if __name__ == "__main__":
    print(json.dumps(collect("--no-gates" not in sys.argv,
                             "--no-sessions" not in sys.argv), indent=2))
