#!/usr/bin/env python3
r"""Generate docs/design/ARCHITECTURE.md from the LIVE stack.

Same convention as gen_compliance_map.py / gen_usecase_register.py: a generated
artifact, never hand-edited. The difference — and it matters — is the source. Those
two generate from files committed to the repo, so CI can regenerate and diff them.
This one reads the RUNNING containers, which CI cannot see. So:

  - it REFUSES to write when the stack is unreachable, rather than emit a document
    that quietly claims nothing is running (a silent empty result is a lie);
  - the output carries the timestamp it was generated at, so a reader can judge
    staleness for themselves;
  - there is deliberately NO CI staleness gate, because CI has no docker daemon.
    Regenerate after changing the stack.

What is DISCOVERED vs AUTHORED is labelled in the output, so the diagram never
implies more certainty than it has: nodes, ports, mounts and workflow state are read
live; the router's internal call fan-out is authored here (it lives in router policy,
not in anything docker exposes).

    python scripts/gen_architecture.py            # writes docs/design/ARCHITECTURE.md
    python scripts/gen_architecture.py --stdout   # print, write nothing

Pure stdlib. Windows + WSL-Docker friendly.
"""
import os, sys, json, glob, argparse, subprocess, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, "n8n-workflows")
OUT = os.path.join(ROOT, "docs", "design", "ARCHITECTURE.md")
OUT_HTML = os.path.join(ROOT, "docs", "design", "architecture.html")
IS_WIN = os.name == "nt"

# AUTHORED: the router's fan-out is enforced by router policy, not visible to docker.
AUTHORED_EDGES = [
    ("router", "claude-runner", "RUNNER_TOKEN"),
    ("router", "gemini-runner", "RUNNER_TOKEN"),
]


def sh(cmd):
    """Run on the docker host — via WSL on Windows. Encoding pinned: the Windows
    ANSI codepage raises UnicodeDecodeError on one stray byte and returns None."""
    # Try the host's own docker FIRST, then WSL. The original order was
    # WSL-only on Windows, which was right when the engine ran inside a WSL
    # distro — and stopped being right the moment ADR 0009 moved it to Docker
    # Desktop. `wsl -e bash -lc "docker ps"` then returned NOTHING, the
    # generator correctly refused to write, and ARCHITECTURE.md quietly froze at
    # its pre-migration state. Nobody noticed, because refusing is silent.
    # bash FIRST on every platform. cmd.exe does not strip the single quotes in
    # a --format string, so `docker ps --format '{{.Names}}'` comes back as
    # "'lanes'" and every name silently fails to match — a subtler failure than
    # the WSL one it replaced, and the reason this order is explicit.
    attempts = [["bash", "-lc", cmd]]
    if IS_WIN:
        attempts.append(["wsl", "-e", "bash", "-lc", cmd])
    last = None
    for argv in attempts:
        last = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if last.returncode == 0 and (last.stdout or "").strip():
            return last
    return last


def discover_services():
    r = sh("docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}'")
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    out = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        name, image = parts[0].strip(), parts[1].strip()
        ports = parts[2].strip() if len(parts) > 2 else ""
        out.append({"name": name, "image": image, "ports": ports,
                    "exposed": "->" in ports})
    return sorted(out, key=lambda s: s["name"])


def discover_mounts(name):
    r = sh(f'docker inspect {name} --format "{{{{range .Mounts}}}}{{{{.Source}}}}|'
           f'{{{{.Destination}}}}|{{{{if .RW}}}}rw{{{{else}}}}ro{{{{end}}}}\\n{{{{end}}}}"')
    rows = []
    for line in (r.stdout or "").splitlines():
        p = line.strip().split("|")
        if len(p) == 3 and p[1]:
            src = p[0].replace("\\", "/")
            src = src.split("/ai-control-plane/")[-1] if "/ai-control-plane/" in src else os.path.basename(src)
            rows.append({"src": src, "dst": p[1], "mode": p[2]})
    return rows


def discover_workflows():
    act = sh("docker exec n8n n8n list:workflow --active=true")
    ina = sh("docker exec n8n n8n list:workflow --active=false")

    def parse(r):
        out = []
        for l in (r.stdout or "").splitlines():
            if "|" in l:
                wid, _, nm = l.partition("|")
                out.append({"id": wid.strip(), "name": nm.strip()})
        return out
    return parse(act), parse(ina)


def repo_workflows():
    rows = []
    for p in sorted(glob.glob(os.path.join(WF_DIR, "*.json"))):
        base = os.path.basename(p)
        txt = open(p, encoding="utf-8").read()
        wid = None
        try:
            wid = json.loads(txt).get("id")
        except json.JSONDecodeError:
            pass
        rows.append({"file": base, "id": wid,
                     "placeholder": "REPLACE_WITH" in txt,
                     "example": ".example." in base})
    return rows


def derive_edges(services, loaded_ids):
    """DISCOVERED n8n->service edges, split by whether the referencing workflow is
    actually LOADED in n8n. A file sitting in the repo is not a live call path —
    conflating the two would make the diagram claim more than is true."""
    names = {s["name"] for s in services if s["name"] != "n8n"}
    live, available = set(), set()
    for p in glob.glob(os.path.join(WF_DIR, "*.json")):
        txt = open(p, encoding="utf-8").read()
        try:
            wid = json.loads(txt).get("id")
        except json.JSONDecodeError:
            wid = None
        bucket = live if (wid and wid in loaded_ids) else available
        for n in names:
            if f"{n}:" in txt:
                bucket.add(n)
    return sorted(live), sorted(available - live)


def mermaid(services, live, available):
    exp = [s for s in services if s["exposed"]]
    inte = [s for s in services if not s["exposed"]]
    L = ["```mermaid", "flowchart LR"]
    L.append('  subgraph HOST["Host — 127.0.0.1 only"]')
    for s in exp:
        L.append(f'    {s["name"].replace("-", "_")}["{s["name"]}<br/>{s["ports"]}"]')
    L.append("  end")
    L.append('  subgraph NET["ai-control-plane_agentnet — internal, no host port"]')
    for s in inte:
        L.append(f'    {s["name"].replace("-", "_")}["{s["name"]}<br/>{s["ports"] or "internal"}"]')
    L.append("  end")
    for tgt in live:
        L.append(f'  n8n ==>|live| {tgt.replace("-", "_")}')
    for tgt in available:
        L.append(f'  n8n -.->|available, not loaded| {tgt.replace("-", "_")}')
    for a, b, tok in AUTHORED_EDGES:
        if any(s["name"] == a for s in services) and any(s["name"] == b for s in services):
            L.append(f'  {a.replace("-", "_")} -.->|{tok}| {b.replace("-", "_")}')
    L.append("```")
    return "\n".join(L)


def render(services, active, inactive, repo, live, available, ts, mounts):
    L = []
    L.append("# Architecture — live stack")
    L.append("")
    L.append("> GENERATED by `scripts/gen_architecture.py` from the **running containers**")
    L.append("> — **do not edit by hand**. Regenerate: `python scripts/gen_architecture.py`.")
    L.append(f"> Generated {ts}.")
    L.append("")
    L.append("Unlike the compliance map and use-case register, this is generated from live")
    L.append("infrastructure rather than committed files, so **CI cannot check it for")
    L.append("staleness** — there is no docker daemon in CI. Judge freshness by the stamp")
    L.append("above and regenerate after changing the stack.")
    L.append("")
    L.append("## Topology")
    L.append("")
    L.append("Nodes, ports and mounts are **discovered** from the running containers.")
    L.append("Thick arrows are **live** call paths — a workflow that is actually loaded in")
    L.append("n8n references that service. Dotted `available` arrows come from workflow")
    L.append("files in the repo that are **not loaded**, so they are not call paths today.")
    L.append("Dotted token arrows are **authored**: the router's fan-out lives in router")
    L.append("policy, which docker does not expose.")
    L.append("")
    L.append(mermaid(services, live, available))
    L.append("")
    L.append("## Services")
    L.append("")
    L.append("| Service | Image | Ports | Reachable from host |")
    L.append("|---|---|---|---|")
    for s in services:
        L.append(f'| `{s["name"]}` | `{s["image"]}` | `{s["ports"] or "internal"}` | '
                 f'{"**yes** (localhost only)" if s["exposed"] else "no"} |')
    L.append("")
    L.append("## What each service can touch")
    L.append("")
    L.append("| Service | Path | Mode |")
    L.append("|---|---|---|")
    for s in services:
        for m in mounts.get(s["name"], []):
            L.append(f'| `{s["name"]}` | `{m["src"]} → {m["dst"]}` | `{m["mode"]}` |')
    L.append("")
    L.append("## Loaded in n8n")
    L.append("")
    if active or inactive:
        L.append("| Workflow id | Name | State |")
        L.append("|---|---|---|")
        for w in active:
            L.append(f'| `{w["id"]}` | {w["name"]} | **active** |')
        for w in inactive:
            L.append(f'| `{w["id"]}` | {w["name"]} | idle |')
    else:
        L.append("_No workflows loaded._")
    L.append("")
    loaded = {w["id"] for w in active + inactive}
    gap = [r for r in repo if not r["example"] and (not r["id"] or r["id"] not in loaded)]
    L.append(f"**{len(loaded)} of {len([r for r in repo if not r['example']])}** "
             "non-example workflow files are loaded.")
    if gap:
        L.append("")
        L.append("| Not loaded | Deploy-ready? |")
        L.append("|---|---|")
        for r in gap:
            L.append(f'| `{r["file"]}` | '
                     f'{"no — contains `REPLACE_WITH`" if r["placeholder"] else "yes"} |')
    L.append("")
    L.append("## Why the Python lanes are not run by n8n")
    L.append("")
    L.append("n8n's image is a hardened image with no package manager and no python, and")
    L.append("no other service in the stack has one. Lanes that must be n8n-driven expose")
    L.append("an HTTP endpoint that n8n calls — see")
    L.append("[0006-python-lanes-run-host-side.md](../decisions/0006-python-lanes-run-host-side.md).")
    L.append("")
    return "\n".join(L) + "\n"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def workflow_schedules():
    """Workflow id -> a short schedule label parsed from the repo files ('' if none)."""
    out = {}
    for p in glob.glob(os.path.join(WF_DIR, "*.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        wid = d.get("id")
        if not wid:
            continue
        label = ""
        for n in d.get("nodes", []):
            if n.get("type") == "n8n-nodes-base.scheduleTrigger":
                iv = (n.get("parameters", {}).get("rule", {}).get("interval") or [{}])[0]
                field, h, m = iv.get("field", ""), iv.get("triggerAtHour"), iv.get("triggerAtMinute", 0)
                if field == "days" and h is not None:
                    label = f"daily {h:02d}:{m:02d}"
                elif field == "weeks":
                    label = f"weekly {h:02d}:{m:02d}" if h is not None else "weekly"
                elif field == "minutes":
                    label = f"every {iv.get('minutesInterval', '?')} min"
                else:
                    label = "scheduled"
        out[wid] = label
    return out


HTML_CSS = """
:root{--ground:#f6f8fa;--panel:#fff;--panel2:#eef2f6;--ink:#16202b;--soft:#55647a;
--line:#dbe3ec;--accent:#b4762c;--accent-s:rgba(180,118,44,.12);--ok:#2f7d55;
--ok-s:rgba(47,125,85,.12);--idle:#78899c;--idle-s:rgba(120,137,156,.14);
--warn:#b4543f;--warn-s:rgba(180,84,63,.12);
--mono:ui-monospace,"Cascadia Mono","SF Mono",Menlo,Consolas,monospace;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;}
@media(prefers-color-scheme:dark){:root{--ground:#0e1319;--panel:#161d26;--panel2:#1c2530;
--ink:#e6edf5;--soft:#93a3b5;--line:#263140;--accent:#d99a4e;--accent-s:rgba(217,154,78,.14);
--ok:#52ab7a;--ok-s:rgba(82,171,122,.15);--idle:#7b8b9d;--idle-s:rgba(123,139,157,.16);
--warn:#d47259;--warn-s:rgba(212,114,89,.15);}}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--ink);
font-family:var(--sans);line-height:1.55;padding:clamp(20px,4vw,52px);}
.page{max-width:1060px;margin:0 auto;display:flex;flex-direction:column;gap:32px;}
h1{font-family:var(--mono);font-size:clamp(21px,3vw,29px);font-weight:600;margin:0;letter-spacing:-.015em;}
h2{font-family:var(--mono);font-size:14px;font-weight:600;margin:0 0 2px;letter-spacing:.04em;
text-transform:uppercase;color:var(--soft);}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;
color:var(--accent);margin:0 0 8px;}
header{border-bottom:1px solid var(--line);padding-bottom:22px;}
p{margin:0;color:var(--soft);max-width:70ch;}
section{display:flex;flex-direction:column;gap:13px;}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px;}
.chip{font-family:var(--mono);font-size:12px;padding:5px 10px;border-radius:3px;
border:1px solid var(--line);background:var(--panel);color:var(--soft);font-variant-numeric:tabular-nums;}
.chip b{color:var(--ink);}.chip.on{border-color:var(--ok);background:var(--ok-s);color:var(--ok);}
.chip.gap{border-color:var(--warn);background:var(--warn-s);color:var(--warn);}
.zone{border:1px solid var(--line);border-radius:5px;background:var(--panel);padding:16px;
display:flex;flex-direction:column;gap:12px;}
.zone.exposed{border-left:3px solid var(--warn);}.zone.internal{border-left:3px solid var(--ok);}
.zone-h{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:baseline;}
.zone-n{font-family:var(--mono);font-size:13px;font-weight:600;}
.zone-note{font-family:var(--mono);font-size:11px;color:var(--soft);}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:9px;}
.svc{border:1px solid var(--line);border-radius:4px;background:var(--panel2);padding:10px 12px;
display:flex;flex-direction:column;gap:4px;}.svc.new{border-color:var(--accent);}
.svc-n{font-family:var(--mono);font-size:13px;font-weight:600;}
.svc-m{font-family:var(--mono);font-size:10.5px;color:var(--soft);word-break:break-all;}
.tag{font-family:var(--mono);font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;
color:var(--accent);background:var(--accent-s);padding:2px 6px;border-radius:2px;align-self:flex-start;}
.mchips{display:flex;flex-wrap:wrap;gap:4px;margin-top:2px;}
.mchip{font-family:var(--mono);font-size:10px;padding:1px 6px;border-radius:2px;}
.m-ro{background:var(--idle-s);color:var(--idle);}.m-rw{background:var(--warn-s);color:var(--warn);}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:5px;background:var(--panel);}
table{width:100%;border-collapse:collapse;font-size:13px;}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);}
th{font-family:var(--mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--soft);}
tr:last-child td{border-bottom:none;}td.m{font-family:var(--mono);font-size:12px;}
.pill{font-family:var(--mono);font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;
padding:2px 8px;border-radius:2px;white-space:nowrap;}
.p-on{background:var(--ok-s);color:var(--ok);}.p-idle{background:var(--idle-s);color:var(--idle);}
.p-gap{background:var(--warn-s);color:var(--warn);}
.flow{display:flex;flex-wrap:wrap;gap:7px;align-items:center;}
.step{border:1px solid var(--line);border-radius:4px;background:var(--panel);padding:8px 11px;
font-family:var(--mono);font-size:12px;}.arrow{color:var(--accent);font-family:var(--mono);}
footer{border-top:1px solid var(--line);padding-top:16px;font-family:var(--mono);font-size:11px;color:var(--soft);}
code{font-family:var(--mono);font-size:.9em;background:var(--panel2);padding:1px 5px;border-radius:3px;color:var(--ink);}
"""


def render_html(services, active, inactive, repo, ts, mounts, schedules):
    exposed = [s for s in services if s["exposed"]]
    internal = [s for s in services if not s["exposed"]]
    loaded = {w["id"] for w in active + inactive}
    non_example = [r for r in repo if not r["example"]]
    gap = [r for r in non_example if not r["id"] or r["id"] not in loaded]

    def card(s):
        new = " new" if s["name"] == "lanes" else ""
        tag = '<span class="tag">new</span>' if s["name"] == "lanes" else ""
        mchips = "".join(
            f'<span class="mchip m-{m["mode"]}">{esc(m["dst"])} {m["mode"]}</span>'
            for m in mounts.get(s["name"], []))
        return (f'<div class="svc{new}">{tag}<span class="svc-n">{esc(s["name"])}</span>'
                f'<span class="svc-m">{esc(s["ports"] or "internal")}</span>'
                f'<span class="svc-m">{esc(s["image"])}</span>'
                + (f'<div class="mchips">{mchips}</div>' if mchips else "") + "</div>")

    rows = []
    for w in active:
        sch = schedules.get(w["id"], "")
        rows.append((w["id"], w["name"], sch or "on demand", '<span class="pill p-on">active</span>'))
    for w in inactive:
        sch = schedules.get(w["id"], "")
        rows.append((w["id"], w["name"], sch or "on demand", '<span class="pill p-idle">loaded</span>'))
    for r in gap:
        pill = ('<span class="pill p-gap">placeholder</span>' if r["placeholder"]
                else '<span class="pill p-gap">not loaded</span>')
        rows.append((r["id"] or "—", r["file"], "—", pill))

    H = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         "<title>AI Control Plane — live architecture</title>",
         f"<style>{HTML_CSS}</style></head><body><div class='page'>"]
    H.append('<header><p class="eyebrow">AI Control Plane · live stack</p>'
             "<h1>Architecture &amp; implementation status</h1>"
             f"<p>Generated from the running containers on {esc(ts)} — "
             "<code>python scripts/gen_architecture.py</code>. Not written from memory: "
             "components, ports, mounts and workflow state are read live.</p>"
             '<div class="chips">'
             f'<span class="chip on"><b>{len(services)}</b> services up</span>'
             f'<span class="chip on"><b>{len(active)}</b> lanes active</span>'
             f'<span class="chip"><b>{len(exposed)}</b> host ports (localhost)</span>'
             f'<span class="chip gap"><b>{len(loaded)} of {len(non_example)}</b> workflows loaded</span>'
             "</div></header>")

    H.append('<section><h2>Trust boundary</h2>'
             "<p>Only host-exposed services are reachable off the box; everything that does "
             "work is internal and token-gated.</p>"
             '<div class="zone exposed"><div class="zone-h"><span class="zone-n">Reachable from host</span>'
             '<span class="zone-note">bound to 127.0.0.1 — not the LAN</span></div>'
             f'<div class="grid">{"".join(card(s) for s in exposed)}</div></div>'
             '<div class="zone internal"><div class="zone-h"><span class="zone-n">Internal — ai-control-plane_agentnet</span>'
             '<span class="zone-note">no host port; each hop carries a token</span></div>'
             f'<div class="grid">{"".join(card(s) for s in internal)}</div></div></section>')

    H.append('<section><h2>Scheduled lane flow</h2>'
             "<p>n8n cannot run Python (hardened image), so it drives the lanes over HTTP — "
             "schedule, log, gate and notification stay in n8n.</p>"
             '<div class="flow">'
             '<span class="step">schedule</span><span class="arrow">→</span>'
             '<span class="step">lanes (python)</span><span class="arrow">→</span>'
             '<span class="step">parse</span><span class="arrow">→</span>'
             '<span class="step">gate</span><span class="arrow">→</span>'
             '<span class="step">Notify</span><span class="arrow">→</span>'
             '<span class="step">Slack</span></div></section>')

    trow = "".join(
        f'<tr><td class="m">{esc(i)}</td><td>{esc(n)}</td><td class="m">{esc(s)}</td><td>{p}</td></tr>'
        for i, n, s, p in rows)
    H.append('<section><h2>Workflows — implementation status</h2>'
             f"<p><b>{len(loaded)} of {len(non_example)}</b> non-example workflow files are "
             "loaded into n8n. Placeholders still carry <code>REPLACE_WITH</code> or need "
             "an external credential.</p>"
             '<div class="scroll"><table><thead><tr><th>Id</th><th>Workflow</th>'
             f"<th>Cadence</th><th>Status</th></tr></thead><tbody>{trow}</tbody></table></div></section>")

    H.append(f'<footer>Generated from <code>docker ps</code>, <code>docker inspect</code> '
             f"and <code>n8n list:workflow</code> against the running stack · {esc(ts)} · "
             "CI cannot check this for staleness (no docker daemon) — regenerate after "
             "changing the stack.</footer>")
    H.append("</div></body></html>")
    return "\n".join(H) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Generate ARCHITECTURE.md from the live stack.")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = ap.parse_args()

    services = discover_services()
    if not services:
        print("REFUSED: no running containers found (is the stack up, and is docker "
              "reachable?). Not writing ARCHITECTURE.md — a generated doc claiming an "
              "empty stack would be worse than none.", file=sys.stderr)
        return 2

    active, inactive = discover_workflows()
    loaded_ids = {w["id"] for w in active + inactive}
    live, available = derive_edges(services, loaded_ids)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    repo = repo_workflows()
    mounts = {s["name"]: discover_mounts(s["name"]) for s in services}   # query docker once
    schedules = workflow_schedules()

    doc = render(services, active, inactive, repo, live, available, ts, mounts)
    html = render_html(services, active, inactive, repo, ts, mounts, schedules)

    if args.stdout:
        print(doc)
        return 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(doc)
    with open(OUT_HTML, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    print(f"wrote {os.path.relpath(OUT, ROOT)} + {os.path.relpath(OUT_HTML, ROOT)}  "
          f"({len(services)} services, {len(active)} active workflow(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
