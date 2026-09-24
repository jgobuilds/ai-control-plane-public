#!/usr/bin/env python3
"""Generate the AI use-case register — an executive-facing conformity inventory.

An EU-AI-Act-style record of every AI use case in the scope tree: its scope,
level, use case, owner, status, DEFAULT risk level, operating mode, and the data
controls (isolation / PII / identity group) that govern it. Cross-cutting lanes
(n8n workflows) are listed too, since a workflow is itself a use case that spans
scopes.

Reads the source-of-truth artifacts and DERIVES everything the way the router
does — nothing is hand-maintained:
  - effective controls: node `controls` override `levelDefaults[level]`
    (mirrors scripts/gen_compose.py `effective_controls`)
  - DEFAULT risk level: data dimension from the scope's pii/isolation
    (silo/block -> 3, warn -> 2, off/none -> 1) with the request `action` /
    `trigger` at their policy defaults; combined the way router/server.js
    `classifyRisk` does — worst single dimension, escalated to `critical` when
    >= 2 dimensions are maxed (3).
  - operating mode: the scope's `controls.mode`, else `modes.default`
    (mirrors router/server.js `resolveMode` with no request override).

Emits BOTH (data is identical):
  - docs/usecase-register.md    Markdown table + a cross-cutting "Lanes" table
  - usecase-register.html       self-contained (inline CSS + click-to-sort JS)

Pure stdlib + yaml (host has Python, not Node). Regenerate on any change to
scopes.json / policy.json / n8n-workflows:
    python scripts/gen_usecase_register.py
"""
import json, os, sys, glob, html

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCOPES_PATH = os.path.join(ROOT, "context", "scopes.json")
POLICY_PATH = os.path.join(ROOT, "router", "policy.json")
LANES_GLOB = os.path.join(ROOT, "n8n-workflows", "*.json")
MD_OUT = os.path.join(ROOT, "docs", "usecase-register.md")
HTML_OUT = os.path.join(ROOT, "usecase-register.html")

STATUSES = ["proposed", "approved", "in-production", "retired"]


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def effective_controls(s, name):
    """Node controls override levelDefaults[level] — same as gen_compose.py."""
    node = s["nodes"][name]
    ctl = dict(s.get("levelDefaults", {}).get(node["level"], {}))
    ctl.update(node.get("controls", {}))
    return ctl


def effective_identity_group(s, name):
    """identityGroup lives at node top level (override) or levelDefaults[level]."""
    node = s["nodes"][name]
    if node.get("identityGroup") is not None:
        return node["identityGroup"]
    return s.get("levelDefaults", {}).get(node["level"], {}).get("identityGroup")


def default_risk(policy, ctl):
    """Mirror router/server.js classifyRisk with no request action/trigger.

    dataKey = "silo" when isolation is silo, else the pii setting; access and
    autonomy fall through to their policy defaults. Level = worst dimension,
    escalated to critical when >= 2 dimensions are 3.
    """
    R = policy["risk"]
    data_key = "silo" if ctl.get("isolation") == "silo" else (ctl.get("pii") or "default")
    dims = {
        "data": R["data"].get(data_key, R["data"]["default"]),
        "access": R["access"]["default"],       # no request action
        "autonomy": R["autonomy"]["default"],    # no request trigger
    }
    base = max(dims.values())
    highs = sum(1 for v in dims.values() if v >= 3)
    idx = 3 if highs >= 2 else base - 1
    idx = max(0, min(3, idx))
    return R["levels"][idx], dims


def effective_mode(policy, ctl):
    """Scope's declared mode if valid, else modes.default (resolveMode, no override)."""
    M = policy["modes"]
    order = M.get("order", [])
    scope_mode = ctl.get("mode")
    return scope_mode if scope_mode in order else M["default"]


def scope_chain(s, name):
    out, cur = [], name
    while cur:
        out.insert(0, cur)
        cur = s["nodes"][cur].get("parent")
    return out


def collect_rows(s, policy):
    rows = []
    for name, node in s["nodes"].items():
        ctl = effective_controls(s, name)
        risk, _dims = default_risk(policy, ctl)
        rows.append({
            "scope": name,
            "level": node["level"],
            "useCase": node.get("useCase", ""),
            "owner": node.get("owner", ""),
            "status": node.get("status", ""),
            "risk": risk,
            "mode": effective_mode(policy, ctl),
            "isolation": ctl.get("isolation", ""),
            "pii": ctl.get("pii", ""),
            "identityGroup": effective_identity_group(s, name) or "",
        })
    # stable, readable order: by scope-chain depth then name
    rows.sort(key=lambda r: (len(scope_chain(s, r["scope"])), r["scope"]))
    return rows


def collect_lanes():
    lanes = []
    for path in sorted(glob.glob(LANES_GLOB)):
        try:
            d = load(path)
        except Exception:
            continue
        lanes.append({
            "file": os.path.basename(path),
            "name": d.get("name", os.path.basename(path)),
            "description": (d.get("meta") or {}).get("description", ""),
        })

    # A lane with no meta.description renders as a BLANK CELL — the register
    # still looks complete, and the reader cannot tell "no description" from
    # "nothing to say". Three lanes shipped that way before anyone noticed, so
    # this is a gate rather than a warning.
    undescribed = [l["file"] for l in lanes if not l["description"].strip()]
    if undescribed:
        raise SystemExit(
            "use-case register: these workflows have no meta.description and "
            "would render as empty rows:\n  "
            + "\n  ".join(undescribed)
            + "\nAdd a meta.description to each — the register is the inventory, "
              "and a blank row is worse than a missing one.")
    return lanes


# ---------- Markdown ----------
def md_escape(v):
    return str(v).replace("|", "\\|")


def render_md(rows, lanes):
    L = []
    L.append("# AI use-case register (conformity inventory)")
    L.append("")
    L.append("> **⚠ Sample configuration.** The scopes, owners, use cases and "
             "statuses below are **illustrative** and describe no real deployment, team "
             "or engagement. Owners are `@example.com` addresses (RFC 2606) rather than "
             "names, because an invented but realistic name cannot be told apart from a "
             "real one. Replace `context/scopes.json` with your own.")
    L.append("")
    L.append("> GENERATED by `scripts/gen_usecase_register.py` from `context/scopes.json`,")
    L.append("> `router/policy.json`, and `n8n-workflows/*.json` — **do not edit by hand**.")
    L.append("> Regenerate: `python scripts/gen_usecase_register.py`. See [USECASE-REGISTER.md](design/USECASE-REGISTER.md).")
    L.append("")
    L.append("An EU-AI-Act-style record of every AI use case, its risk tier, operating")
    L.append("mode, owner, and status. *Risk (default)* is the risk level of a baseline")
    L.append("request against the scope (data dimension from the scope's PII/isolation; the")
    L.append("request `action`/`trigger` at policy defaults) — a live request can only score")
    L.append("higher. *Mode* is the scope's operating mode (who leads / who validates).")
    L.append("")
    cols = ["Scope", "Level", "Use case", "Owner", "Status",
            "Risk (default)", "Mode", "Isolation", "PII", "Identity group"]
    L.append("| " + " | ".join(cols) + " |")
    L.append("|" + "|".join("---" for _ in cols) + "|")
    for r in rows:
        L.append("| " + " | ".join(md_escape(v) for v in [
            r["scope"], r["level"], r["useCase"] or "—", r["owner"] or "—",
            r["status"] or "—", r["risk"], r["mode"], r["isolation"] or "—",
            r["pii"] or "—", r["identityGroup"] or "—",
        ]) + " |")
    L.append("")
    L.append("## Lanes (cross-cutting use cases)")
    L.append("")
    L.append("Each n8n workflow is a use case that spans scopes — the automation lanes the")
    L.append("governance layer runs on.")
    L.append("")
    L.append("| Lane | Description |")
    L.append("|---|---|")
    for lane in lanes:
        L.append(f"| {md_escape(lane['name'])} | {md_escape(lane['description'])} |")
    L.append("")
    return "\n".join(L)


# ---------- HTML ----------
HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI use-case register — the control plane</title>
<style>
  .sample{{border:1px solid #a8690a;border-left:4px solid #a8690a;border-radius:8px;padding:.7rem .9rem;margin:0 0 1rem;font-size:.94rem}}
  @media(prefers-color-scheme:dark){{.sample{{border-color:#d69e2e}}}}
  :root {{
    --bg: #ffffff; --fg: #1c1f26; --muted: #5b6472; --line: #e2e6ec;
    --head: #f4f6f9; --accent: #2f6feb; --chip: #eef1f6;
    --low: #2f8f4e; --medium: #b8860b; --high: #c9541b; --critical: #c0392b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #14171c; --fg: #e6e9ee; --muted: #9aa4b2; --line: #2a2f38;
      --head: #1c2129; --accent: #6aa0ff; --chip: #232a34;
      --low: #5fd08a; --medium: #e0b64a; --high: #f0925a; --critical: #f27a6e;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }}
  .wrap {{ max-width: 1200px; margin: 0 auto; padding: 28px 20px 60px; }}
  h1 {{ font-size: 24px; margin: 0 0 6px; }}
  .lead {{ color: var(--muted); max-width: 70ch; margin: 0 0 4px; }}
  .gen {{ color: var(--muted); font-size: 12.5px; margin: 10px 0 20px; }}
  .gen code {{ background: var(--chip); padding: 1px 5px; border-radius: 4px; }}
  h2 {{ font-size: 18px; margin: 34px 0 10px; }}
  .scroll {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; }}
  table {{ border-collapse: collapse; width: 100%; min-width: 720px; }}
  th, td {{ text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line);
    white-space: nowrap; }}
  td.wrap-cell {{ white-space: normal; min-width: 220px; }}
  thead th {{ background: var(--head); position: sticky; top: 0; cursor: pointer;
    user-select: none; font-size: 13px; letter-spacing: .02em; }}
  thead th.nosort {{ cursor: default; }}
  thead th .arrow {{ color: var(--muted); font-size: 11px; margin-left: 5px; }}
  tbody tr:hover {{ background: var(--head); }}
  code, .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 13px; }}
  .chip {{ display: inline-block; padding: 1px 8px; border-radius: 999px;
    background: var(--chip); font-size: 12.5px; }}
  .risk {{ font-weight: 600; }}
  .risk-low {{ color: var(--low); }} .risk-medium {{ color: var(--medium); }}
  .risk-high {{ color: var(--high); }} .risk-critical {{ color: var(--critical); }}
  .st-in-production {{ color: var(--low); font-weight: 600; }}
  .st-approved {{ color: var(--accent); }}
  .st-proposed {{ color: var(--muted); }}
  .st-retired {{ color: var(--muted); text-decoration: line-through; }}
  .muted {{ color: var(--muted); }}
</style>
</head>
<body>
<div class="wrap">
  <h1>AI use-case register</h1>
  <p class="lead">Conformity inventory — every AI use case in the scope tree with its
    risk tier, operating mode, owner, and status. EU-AI-Act-style record.</p>
  <p class="sample"><strong>&#9888; Sample configuration.</strong> The scopes, owners,
    use cases and statuses below are <strong>illustrative</strong> and describe no real
    deployment, team or engagement. Owners are <code>@example.com</code> addresses
    (RFC&nbsp;2606) rather than names, because an invented but realistic name cannot be
    told apart from a real one. Replace <code>context/scopes.json</code> with your own.</p>
  <p class="gen">Generated by <code>scripts/gen_usecase_register.py</code> from
    <code>context/scopes.json</code>, <code>router/policy.json</code>, and
    <code>n8n-workflows/*.json</code>. Do not edit by hand — click any column header to sort.
    <em>Risk (default)</em> is a baseline request's risk level; a live request can only score higher.</p>

  <h2>Use cases by scope</h2>
  <div class="scroll">
    <table id="scopes">
      <thead><tr>{scope_head}</tr></thead>
      <tbody>{scope_rows}</tbody>
    </table>
  </div>

  <h2>Lanes (cross-cutting use cases)</h2>
  <p class="muted" style="margin-top:-2px">Each n8n workflow is a use case that spans scopes.</p>
  <div class="scroll">
    <table id="lanes">
      <thead><tr>{lane_head}</tr></thead>
      <tbody>{lane_rows}</tbody>
    </table>
  </div>
</div>
<script>
(function () {{
  function cellVal(row, i) {{
    var c = row.cells[i];
    return (c.getAttribute('data-sort') || c.textContent || '').trim().toLowerCase();
  }}
  document.querySelectorAll('table').forEach(function (table) {{
    var dir = {{}};
    table.querySelectorAll('thead th').forEach(function (th, i) {{
      if (th.classList.contains('nosort')) return;
      th.addEventListener('click', function () {{
        var body = table.tBodies[0];
        var rows = Array.prototype.slice.call(body.rows);
        var asc = dir[i] = !dir[i];
        rows.sort(function (a, b) {{
          var x = cellVal(a, i), y = cellVal(b, i);
          var nx = parseFloat(x), ny = parseFloat(y);
          if (!isNaN(nx) && !isNaN(ny)) return asc ? nx - ny : ny - nx;
          return asc ? x.localeCompare(y) : y.localeCompare(x);
        }});
        rows.forEach(function (r) {{ body.appendChild(r); }});
        table.querySelectorAll('thead th .arrow').forEach(function (a) {{ a.textContent = ''; }});
        var arr = th.querySelector('.arrow');
        if (arr) arr.textContent = asc ? '▲' : '▼';
      }});
    }});
  }});
}})();
</script>
</body>
</html>
"""

RISK_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def render_html(rows, lanes):
    scope_cols = ["Scope", "Level", "Use case", "Owner", "Status",
                  "Risk (default)", "Mode", "Isolation", "PII", "Identity group"]
    scope_head = "".join(
        f'<th>{html.escape(c)}<span class="arrow"></span></th>' for c in scope_cols)

    def td(v, cls="", sort=None, mono=False):
        attr = f' class="{cls}"' if cls else ""
        s = f' data-sort="{html.escape(str(sort))}"' if sort is not None else ""
        inner = html.escape(str(v))
        if mono:
            inner = f'<span class="mono">{inner}</span>'
        return f"<td{attr}{s}>{inner}</td>"

    scope_rows = []
    for r in rows:
        risk = r["risk"]
        status = r["status"]
        st_cls = f"st-{status}" if status else "muted"
        cells = [
            td(r["scope"], mono=True),
            td(r["level"]),
            td(r["useCase"] or "—", cls="wrap-cell"),
            td(r["owner"] or "—"),
            f'<td class="{st_cls}">{html.escape(status or "—")}</td>',
            f'<td class="risk risk-{risk}" data-sort="{RISK_RANK[risk]}">{html.escape(risk)}</td>',
            td(r["mode"], mono=True),
            td(r["isolation"] or "—"),
            td(r["pii"] or "—"),
            td(r["identityGroup"] or "—", cls="muted", mono=True),
        ]
        scope_rows.append("<tr>" + "".join(cells) + "</tr>")

    lane_head = ('<th>Lane<span class="arrow"></span></th>'
                 '<th class="nosort">Description</th>')
    lane_rows = []
    for lane in lanes:
        lane_rows.append(
            "<tr>" + td(lane["name"], mono=True)
            + td(lane["description"], cls="wrap-cell muted") + "</tr>")

    return HTML_TEMPLATE.format(
        scope_head=scope_head, scope_rows="".join(scope_rows),
        lane_head=lane_head, lane_rows="".join(lane_rows))


def main():
    s = load(SCOPES_PATH)
    policy = load(POLICY_PATH)
    rows = collect_rows(s, policy)
    lanes = collect_lanes()

    os.makedirs(os.path.dirname(MD_OUT), exist_ok=True)
    with open(MD_OUT, "w", encoding="utf-8") as f:
        f.write(render_md(rows, lanes))
    with open(HTML_OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_html(rows, lanes))

    print(f"Wrote {os.path.relpath(MD_OUT, ROOT)} and {os.path.relpath(HTML_OUT, ROOT)}")
    print(f"Scopes: {len(rows)}  Lanes: {len(lanes)}")
    for r in rows:
        print(f"  {r['scope']:12s} {r['level']:11s} risk={r['risk']:8s} "
              f"mode={r['mode']:10s} status={r['status'] or '-'}")


if __name__ == "__main__":
    main()
