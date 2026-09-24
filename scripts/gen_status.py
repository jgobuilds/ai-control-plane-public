#!/usr/bin/env python3
"""Render the stack's status as one self-contained HTML page.

WHAT WAS MISSING. Everything here already existed as a script with an exit code
or a line in the ledger, but only as a PUSH: alerts arrive when something breaks.
There was no pull-based view — no way to ask "is the stack healthy" and get an
answer. `agentsview` looked like that view and was not: it showed Claude Code
usage analytics, nothing about lanes, gates, the ledger or the runners, and it
had been reading 0 of 71 session files for weeks.

So this replaces it and covers both halves: the ops view that was missing, and
the session analytics it was supposed to provide.

    python scripts/gen_status.py                 # write status.html
    python scripts/gen_status.py --open          # and open it
    python scripts/gen_status.py --json          # data only, no HTML
    python scripts/gen_status.py --no-gates      # skip the slow checks

NEUTRAL BY CONSTRUCTION. This engine ships brand-neutral, so the page uses plain
CSS variables and no brand tokens — the same rule the other generated HTML
follows. It is also a single file with no external requests: no CDN, no font, no
script from anywhere. A dashboard that phones out to render is a dashboard that
breaks when the thing it monitors breaks the network.

Pure stdlib.
"""
from __future__ import annotations
import argparse, html, importlib.util, json, os, sys, webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "status.html")

_spec = importlib.util.spec_from_file_location(
    "status_data", os.path.join(ROOT, "scripts", "status_data.py"))
SD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(SD)

CSS = """
:root{--ground:#f7f8fa;--panel:#fff;--panel2:#f0f2f5;--ink:#11151a;--muted:#5b6572;
--line:#dde2e8;--ok:#137a4a;--bad:#b3261e;--warn:#8a6100;--idle:#6b7480}
@media(prefers-color-scheme:dark){:root{--ground:#0e1319;--panel:#161d26;--panel2:#1c2530;
--ink:#e6edf3;--muted:#93a1b1;--line:#2a3542;--ok:#3fb27f;--bad:#f2708a;--warn:#e0b341;--idle:#7d8898}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:22px;margin:0 0 2px} h2{font-size:15px;margin:28px 0 10px;
text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.sub{color:var(--muted);font-size:13px;margin-bottom:18px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(210px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi{font-size:26px;font-weight:600;letter-spacing:-.02em}
.kpi small{font-size:12px;font-weight:400;color:var(--muted);display:block;
text-transform:uppercase;letter-spacing:.07em;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--panel);
border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);font-size:14px}
th{background:var(--panel2);font-weight:600;color:var(--muted);
text-transform:uppercase;letter-spacing:.06em;font-size:11px}
tr:last-child td{border-bottom:0}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600}
.pass{background:color-mix(in srgb,var(--ok) 16%,transparent);color:var(--ok)}
.fail{background:color-mix(in srgb,var(--bad) 16%,transparent);color:var(--bad)}
.skip{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}
.idle{background:color-mix(in srgb,var(--idle) 18%,transparent);color:var(--idle)}
.muted{color:var(--muted)} .right{text-align:right}
.bars{display:flex;flex-direction:column;gap:6px}
.bar{display:grid;grid-template-columns:150px 1fr 46px;gap:10px;align-items:center;font-size:13px}
.bar .t{height:9px;background:var(--panel2);border-radius:99px;overflow:hidden}
.bar .t i{display:block;height:100%;background:var(--ok);border-radius:99px}
.heat{display:flex;gap:3px;flex-wrap:wrap}
.heat i{width:11px;height:11px;border-radius:2px;background:var(--panel2);display:block}
.note{font-size:12px;color:var(--muted);margin-top:8px}
.scroll{overflow-x:auto}
"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


def pill(state):
    cls = {"pass": "pass", "fail": "fail", "skipped": "skip", "unknown": "skip"}.get(state, "idle")
    return f'<span class="pill {cls}">{esc(state)}</span>'


def bars(d, limit=8):
    if not d:
        return '<div class="muted">none</div>'
    top = list(d.items())[:limit]
    hi = max(v for _, v in top) or 1
    rows = "".join(
        f'<div class="bar"><span class="mono">{esc(k)}</span>'
        f'<span class="t"><i style="width:{max(3, int(100 * v / hi))}%"></i></span>'
        f'<span class="right mono">{v}</span></div>' for k, v in top)
    return f'<div class="bars">{rows}</div>'


def heat(days, limit=120):
    if not days:
        return '<div class="muted">no activity recorded</div>'
    items = list(days.items())[-limit:]
    hi = max(v for _, v in items) or 1
    cells = "".join(
        '<i title="%s: %d" style="background:color-mix(in srgb,var(--ok) %d%%,var(--panel2))"></i>'
        % (esc(d), v, min(100, 12 + int(88 * v / hi))) for d, v in items)
    return f'<div class="heat">{cells}</div>'


def render(d):
    g = d["gates"]["items"]
    fails = [x for x in g if x["state"] == "fail"]
    cont = d["containers"].get("items", [])
    up = [c for c in cont if c["state"] == "running"]
    led, ses, lanes = d["ledger"], d["sessions"], d["lanes"]

    headline = ("all clear" if not fails else
                f"{len(fails)} gate{'s' if len(fails) != 1 else ''} failing")
    kpi = [
        (headline, "status"),
        (f"{len(up)}/{len(cont)}", "containers up"),
        (led.get("records", 0), "ledger records"),
        (led.get("blocked", 0), "blocked (recent)"),
        (f'${led.get("notionalUsd", 0):,.2f}', "notional cost"),
        (ses.get("sessions", 0), "agent sessions"),
    ]
    cards = "".join(f'<div class="card"><div class="kpi">{esc(v)}<small>{esc(l)}</small></div></div>'
                    for v, l in kpi)

    gate_rows = "".join(
        f'<tr><td>{esc(x["name"])}</td><td>{pill(x["state"])}</td>'
        f'<td class="mono muted">{esc(x["detail"])[:110]}</td></tr>' for x in g)

    cont_rows = "".join(
        f'<tr><td class="mono">{esc(c["name"])}</td>'
        f'<td>{pill("pass" if c["state"] == "running" else "fail")}</td>'
        f'<td class="muted">{esc(c["status"])}</td></tr>' for c in cont) or \
        '<tr><td colspan="3" class="muted">docker unavailable</td></tr>'

    lane_rows = "".join(
        f'<tr><td class="mono">{esc(i["lane"])}</td><td class="muted">{esc(i["kind"])}</td>'
        f'<td class="mono muted">{esc(i["last"] or "never recorded")}</td></tr>'
        for i in lanes.get("items", []))
    lane_note = ("" if lanes.get("stateFound") else
                 '<div class="note">No scheduler state file yet — n8n still owns these '
                 'schedules, so "never recorded" here means the replacement scheduler has '
                 'not run, not that the lane has not.</div>')

    ses_block = (f'<div class="muted">{esc(ses["error"])}</div>' if ses.get("error") else
                 f"""<div class="grid">
      <div class="card"><div class="kpi">{ses.get('sessions',0)}<small>sessions</small></div></div>
      <div class="card"><div class="kpi">{ses.get('messages',0)}<small>messages</small></div></div>
      <div class="card"><div class="kpi">{ses.get('projects',0)}<small>projects</small></div></div>
      <div class="card"><div class="kpi">{ses.get('activeDays',0)}<small>active days</small></div></div>
      <div class="card"><div class="kpi">{ses.get('outputTokens',0):,}<small>output tokens</small></div></div>
    </div>
    <h2>Activity</h2>{heat(ses.get('days'))}
    <h2>By model</h2>{bars(ses.get('byModel'))}
    <h2>By project</h2>{bars(ses.get('byProject'))}""")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Control plane — status</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Control plane — status</h1>
<div class="sub">Generated {esc(d['generated'])} · regenerate with
<span class="mono">python scripts/gen_status.py</span></div>
<div class="grid">{cards}</div>

<h2>Gates</h2><div class="scroll"><table>
<tr><th>Check</th><th>State</th><th>Last line</th></tr>{gate_rows}</table></div>
<div class="note">Each row is an existing check script, judged by its exit code. A check that
cannot run is <b>skipped</b>, never green — a gate reporting pass while measuring nothing is
the failure mode this repo keeps finding.</div>

<h2>Containers</h2><div class="scroll"><table>
<tr><th>Name</th><th>State</th><th>Status</th></tr>{cont_rows}</table></div>

<h2>Scheduled lanes</h2><div class="scroll"><table>
<tr><th>Lane</th><th>Kind</th><th>Last run</th></tr>{lane_rows}</table></div>{lane_note}

<h2>Router decisions <span class="muted">(last {led.get('window',0)} of {led.get('records',0)})</span></h2>
<div class="grid">
  <div class="card"><div class="kpi">{led.get('blocked',0)}<small>blocked</small></div></div>
  <div class="card"><div class="kpi">{led.get('escalations',0)}<small>vendor escalations</small></div></div>
  <div class="card"><div class="kpi">${led.get('notionalUsd',0):,.2f}<small>notional cost</small></div></div>
</div>
<h2>By scope</h2>{bars(led.get('byScope'))}
<h2>By model</h2>{bars(led.get('byModel'))}
<h2>Why blocked</h2>{bars(led.get('blockedReasons'))}
<h2>Verify status</h2>{bars(led.get('verifyStatus'))}
<div class="note">Notional cost is what this WOULD cost at metered API rates — the runners are
subscription-billed, so it is a yardstick, not money spent. See AIOPS.md before putting it on a
dashboard someone reads as spend.</div>

<h2>Agent sessions</h2>{ses_block}
<div class="note">Read from the Claude Code transcripts in the runner's config volume. This is the
half <span class="mono">agentsview</span> used to show, and it is retrospective usage — it says
nothing about whether the stack is healthy right now. That is what everything above is for.</div>
</div></body></html>"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render the stack status page.")
    ap.add_argument("--json", action="store_true", help="print data, write no HTML")
    ap.add_argument("--no-gates", action="store_true", help="skip the slow check scripts")
    ap.add_argument("--no-sessions", action="store_true")
    ap.add_argument("--open", action="store_true", help="open the page after writing")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args(argv)

    data = SD.collect(with_gates=not args.no_gates, with_sessions=not args.no_sessions)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(data))
    fails = [x for x in data["gates"]["items"] if x["state"] == "fail"]
    print(f"wrote {args.out}")
    print(f"  gates: {len(data['gates']['items']) - len(fails)} ok, {len(fails)} failing")
    print(f"  containers: {sum(1 for c in data['containers'].get('items', []) if c['state'] == 'running')} running")
    print(f"  ledger: {data['ledger'].get('records', 0)} records, "
          f"{data['ledger'].get('blocked', 0)} blocked in the window")
    if args.open:
        webbrowser.open("file://" + os.path.abspath(args.out))
    # Exit non-zero when a gate is failing, so this is usable as a check too.
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
