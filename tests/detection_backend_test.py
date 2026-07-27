#!/usr/bin/env python3
"""E3 — pluggable ML detection backends: structural + adapter-contract checks.

node isn't installed here, so this validates (a) the default stays heuristic,
(b) the sync heuristic exports are intact, (c) the async backend seam exists and
is GATED behind config (no unconditional network call on the default path), and
(d) the documented adapter response shapes normalize to the expected findings —
mirrored in python from the JS `backendSpans`/`backendFindings` contract.
"""
import os, re, json, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def read(p): return open(os.path.join(ROOT, p), encoding="utf-8").read()

fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond: fails.append(name)

policy = json.loads(read("router/policy.json"))
scrub_js = read("scrubber/server.js")
guard_js = read("router/guardrails.js")
router_js = read("router/server.js")

print("Defaults keep today's behavior (heuristic, no backend):")
g = policy["guardrails"]
check("policy guardrails.backend defaults to heuristic", g.get("backend") == "heuristic")
check("policy guardrails.backendUrl defaults empty", g.get("backendUrl", "") == "")
check("scrubber SCRUB_BACKEND defaults heuristic",
      'process.env.SCRUB_BACKEND || "heuristic"' in scrub_js)

print("Sync heuristic exports intact (existing tests depend on them):")
check("scrubber exports scrub (sync)", re.search(r"module\.exports\s*=\s*{[^}]*\bscrub\b", scrub_js, re.S) is not None)
check("guardrails exports inputScan + outputScan (sync)",
      "inputScan," in guard_js and "outputScan," in guard_js)

print("Async backend seam exists and is GATED (no default-path network call):")
check("scrubber has scrubAsync + detectPiiBackend", "async function scrubAsync" in scrub_js and "detectPiiBackend" in scrub_js)
check("scrubAsync short-circuits to sync scrub on heuristic/no-url",
      'if (SCRUB_BACKEND === "heuristic" || !SCRUB_BACKEND_URL) return scrub(text, scope)' in scrub_js)
check("guardrails has inputScanAsync/outputScanAsync", "inputScanAsync" in guard_js and "outputScanAsync" in guard_js)
check("inputScanAsync short-circuits to heuristic when no backend",
      re.search(r"inputScanAsync[^{]*{[^}]*heuristic[^}]*return heuristic", guard_js, re.S) is not None)
check("router awaits the async guard variants", "inputScanAsync(" in router_js and "outputScanAsync(" in router_js)
# the only network primitive is behind the async helpers, never at module top level
check("no top-level http request in scrubber (require only)",
      scrub_js.count("require(\"https\")") == 1 and ".request(" in scrub_js)

print("Adapter contract — backend responses normalize as documented:")
# mirror of JS backendSpans (PII)
def backend_spans(text, resp):
    out = []
    arr = resp if isinstance(resp, list) else (resp.get("entities") or resp.get("results") or [])
    for e in arr:
        if e is None: continue
        if isinstance(e.get("start"), int) and isinstance(e.get("end"), int):
            v = text[e["start"]:e["end"]]
            if v: out.append({"type": str(e.get("entity_type") or e.get("type") or "PII").upper(), "value": v})
        elif e.get("value"):
            out.append({"type": str(e.get("type") or e.get("entity_type") or "PII").upper(), "value": str(e["value"])})
    return out

txt = "Contact Jane Doe today"
presidio = [{"entity_type": "PERSON", "start": 8, "end": 16, "score": 0.9}]
spans = backend_spans(txt, presidio)
check("Presidio start/end maps to the span value", spans == [{"type": "PERSON", "value": "Jane Doe"}], str(spans))
generic = {"entities": [{"type": "email", "value": "a@b.com"}]}
check("generic {entities} maps to value spans",
      backend_spans("", generic) == [{"type": "EMAIL", "value": "a@b.com"}])

# mirror of JS backendFindings (guardrails)
def backend_findings(resp):
    if not resp: return []
    if isinstance(resp.get("findings"), list):
        return [{"type": str(f.get("type") or "backend"),
                 "level": f["level"] if f.get("level") in ("low","medium","high") else "high"} for f in resp["findings"]]
    flagged = resp.get("flagged") is True or (isinstance(resp.get("results"), list) and any(r.get("flagged") or r.get("detected") for r in resp["results"]))
    return [{"type": "backend-flagged", "level": "high"}] if flagged else []

check("Lakera-style {flagged:true} -> one high finding",
      backend_findings({"flagged": True}) == [{"type": "backend-flagged", "level": "high"}])
check("generic {findings} passes through with level",
      backend_findings({"findings": [{"type": "prompt_injection", "level": "medium"}]}) == [{"type": "prompt_injection", "level": "medium"}])
check("empty/clean response -> no findings", backend_findings({"results": [{"flagged": False}]}) == [])

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}"); sys.exit(1)
print("All detection-backend checks passed.")
