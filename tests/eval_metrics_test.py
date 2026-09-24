#!/usr/bin/env python3
"""Tests for scripts/eval_metrics.py — the eval / drift lane.

Builds synthetic audit ledgers in the router's EXACT chained format
("<hash> <json>", hash = sha256(prevHash + json), each record carrying its own
prevHash) so the script's chain-verify passes, then runs eval_metrics.py --format
json against them and asserts:

  1. computed leverage / verify-pass / change-failure / block-rate / block-reason
     / guardrail / PII / approval / risk-mix / tier-mix match HAND calculations,
  2. a deliberately drifting series (verify-pass collapses second half) raises a
     drift WARN flag,
  3. a tampered ledger is REFUSED (chain_ok:false, non-zero exit) — metrics are
     never computed on untrusted data.

    python tests/eval_metrics_test.py     # exits non-zero on any failure
"""
import os, sys, json, hashlib, tempfile, subprocess, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL = os.path.join(ROOT, "scripts", "eval_metrics.py")

NOW = datetime.datetime.now(datetime.timezone.utc)


def sha256(s):
    return hashlib.sha256(s.encode()).hexdigest()


def days_ago(n):
    dt = NOW - datetime.timedelta(days=n)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000") + "Z"


def build_chain(records):
    """Mirror router auditRecord: append prevHash, hash the raw json substring."""
    lines, prev = [], "GENESIS"
    for r in records:
        r = dict(r, prevHash=prev)
        j = json.dumps(r)
        h = sha256(prev + j)
        lines.append(f"{h} {j}")
        prev = h
    return "\n".join(lines) + "\n"


def write(tmp, text):
    p = os.path.join(tmp, "decisions.jsonl")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return p


def run(path, *extra):
    return subprocess.run([sys.executable, EVAL, path, "--format", "json", *extra],
                          capture_output=True, text=True)


fails = []
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def r(ts, **kw):
    base = {"ts": ts}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Dataset A — hand-calculated aggregate metrics. 10 records, all within window.
#   held (leverage numerator): recs with verdict==true OR (verdict null & not blocked)
#     -> #1,#2,#10 (true) + #4,#5 (null,not blocked) = 5  -> leverage 50%
#   verdicts: true=3 (#1,#2,#10) false=1 (#3) judged=4 -> verify-pass 75%, change-fail 25%
#   blocked=4 (#6 halted, #7 approval-required, #8 guardrail-input, #9 pii) -> block 40%
#   guardrail-finding: 1 (#8) -> 10%   pii-block: 1 (#9) -> 10%   approval: 1 (#10) -> 10%
#   risk mix: low 3 (#4,#5,#6), medium 2 (#2,#8), high 3 (#1,#3,#9), critical 2 (#7,#10)
#   tier mix: t0 2 (#4,#5), t1 1 (#2), t2 1 (#1), t3 2 (#3,#10)
A = [
    r(days_ago(9),  verdict=True,  risk={"level": "high"},     tier="t2", provider="claude", model="sonnet"),
    r(days_ago(9),  verdict=True,  risk={"level": "medium"},   tier="t1", provider="claude", model="haiku"),
    r(days_ago(8),  verdict=False, risk={"level": "high"},     tier="t3", provider="claude", model="opus"),
    r(days_ago(8),  verdict=None,  risk={"level": "low"},      tier="t0", provider="claude", model="rules"),
    r(days_ago(7),  verdict=None,  risk={"level": "low"},      tier="t0", provider="claude", model="rules"),
    r(days_ago(7),  blocked="halted",            risk={"level": "low"}),
    r(days_ago(6),  blocked="approval-required", risk={"level": "critical"}),
    r(days_ago(6),  blocked="guardrail-input",   risk={"level": "medium"}, guardrails={"types": ["injection"], "total": 1}),
    r(days_ago(5),  blocked="error:PII detected (EMAIL) and scope blocks raw PII", risk={"level": "high"}),
    r(days_ago(5),  verdict=True,  risk={"level": "critical"}, tier="t3", provider="claude", model="opus", approved=True),
]

with tempfile.TemporaryDirectory() as tmp:
    p = write(tmp, build_chain(A))
    res = run(p, "--days", "30")
    check("dataset A: exit 0", res.returncode == 0, res.stderr.strip())
    try:
        out = json.loads(res.stdout)
    except Exception as e:
        out = {}
        check("dataset A: parses json", False, f"{e}: {res.stdout[:200]}")

    if out:
        check("chain_ok true", out.get("chain_ok") is True)
        check("records_in_window == 10", out.get("records_in_window") == 10, str(out.get("records_in_window")))
        m = out.get("metrics", {})
        check("agentic_leverage == 50.0%", m.get("agentic_leverage_pct") == 50.0, str(m.get("agentic_leverage_pct")))
        check("verify_pass == 75.0%", m.get("verify_pass_pct") == 75.0, str(m.get("verify_pass_pct")))
        check("change_failure == 25.0%", m.get("change_failure_pct") == 25.0, str(m.get("change_failure_pct")))
        check("block_rate == 40.0%", m.get("block_rate_pct") == 40.0, str(m.get("block_rate_pct")))
        br = m.get("block_reasons", {})
        check("block reason halted == 1", br.get("halted", {}).get("count") == 1)
        check("block reason approval-required == 1", br.get("approval-required", {}).get("count") == 1)
        check("block reason guardrail-input == 1", br.get("guardrail-input", {}).get("count") == 1)
        check("block reason pii == 1 (folded from error:PII)", br.get("pii", {}).get("count") == 1)
        check("guardrail_finding == 10.0%", m.get("guardrail_finding_pct") == 10.0, str(m.get("guardrail_finding_pct")))
        check("pii_block == 10.0%", m.get("pii_block_pct") == 10.0, str(m.get("pii_block_pct")))
        check("approval == 10.0%", m.get("approval_pct") == 10.0, str(m.get("approval_pct")))
        rm = m.get("risk_level_mix", {})
        check("risk low == 3", rm.get("low", {}).get("count") == 3, str(rm.get("low")))
        check("risk medium == 2", rm.get("medium", {}).get("count") == 2)
        check("risk high == 3", rm.get("high", {}).get("count") == 3)
        check("risk critical == 2", rm.get("critical", {}).get("count") == 2)
        tm = m.get("tier_mix", {})
        check("tier t0 == 2", tm.get("t0", {}).get("count") == 2, str(tm))
        check("tier t3 == 2", tm.get("t3", {}).get("count") == 2)

    # ----------------------------------------------------------------------- #
    # scope filter: restrict to a scope with 0 records -> empty window handled.
    A2 = [dict(x, scope="jane") for x in A]
    p2 = write(tmp, build_chain(A2))
    res_s = run(p2, "--days", "30", "--scope", "nonexistent")
    out_s = json.loads(res_s.stdout)
    check("scope filter: empty window handled gracefully",
          out_s.get("records_in_window") == 0 and "note" in out_s, res_s.stdout[:200])

    # ----------------------------------------------------------------------- #
    # Dataset B — deliberate DRIFT. First half: 4x verdict true (verify-pass 100%).
    # Second half: 1 true + 4 false (verify-pass 20%). Drop of 80pp -> WARN.
    B = (
        [r(days_ago(25), verdict=True, risk={"level": "low"}, tier="t1") for _ in range(4)] +
        [r(days_ago(3),  verdict=True, risk={"level": "low"}, tier="t1")] +
        [r(days_ago(2),  verdict=False, risk={"level": "low"}, tier="t1") for _ in range(4)]
    )
    pB = write(tmp, build_chain(B))
    resB = run(pB, "--days", "30")
    outB = json.loads(resB.stdout)
    check("drift: flag raised", outB.get("drift") is True, str(outB.get("subwindows")))
    metrics_flagged = [f.get("metric") for f in outB.get("drift_flags", [])]
    check("drift: verify_pass_pct flagged", "verify_pass_pct" in metrics_flagged, str(metrics_flagged))

    # ----------------------------------------------------------------------- #
    # Tamper: flip a byte inside a record's json — chain must break, be REFUSED.
    intact = build_chain(A)
    lines = intact.strip("\n").split("\n")
    lines[2] = lines[2].replace('"tier": "t3"', '"tier": "t0"')  # silent downgrade attempt
    pT = write(tmp, "\n".join(lines) + "\n")
    resT = run(pT, "--days", "30")
    check("tamper: non-zero exit (refused)", resT.returncode != 0, str(resT.returncode))
    try:
        outT = json.loads(resT.stdout)
        check("tamper: chain_ok false", outT.get("chain_ok") is False, resT.stdout[:200])
        check("tamper: metrics NOT computed", "metrics" not in outT)
    except Exception as e:
        check("tamper: emits valid json", False, str(e))

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("All eval_metrics checks passed.")
