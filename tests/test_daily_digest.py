#!/usr/bin/env python3
"""Corpus for the daily digest formatter.

The digest is instrumentation, so it lives under the diagnosability contract:
it must NEVER raise, and it must capture the deciding facts (which anomalies,
what activity). These cases pin the decisions — post vs skip, warn vs info,
anomaly-forward ordering, coverage-before-total, and the broken-chain refusal —
so a regression makes the test fail, not a human.

    python -m unittest tests.test_daily_digest
"""
import os, sys, datetime, importlib.util, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)  # so daily_digest's `import eval_metrics` resolves

_spec = importlib.util.spec_from_file_location(
    "daily_digest", os.path.join(SCRIPTS, "daily_digest.py"))
dd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dd)

MON = datetime.date(2026, 7, 20)  # a Monday (weekday 0 == default heartbeat)
WED = datetime.date(2026, 7, 22)  # a Wednesday


def _res(*, records=3, metrics=None, drift=None, chain_ok=True, note=False):
    r = {"chain_ok": chain_ok,
         "window": {"since": "2026-07-22T00:00:00+00:00",
                    "until": "2026-07-23T00:00:00+00:00", "scope": "(all)"},
         "records_in_window": records}
    if note:
        r["note"] = "no data yet (empty window)"
    if metrics is not None:
        r["metrics"] = metrics
    if drift is not None:
        r["drift_flags"] = drift
    return r


def _metrics(*, attempted=3, completed=3, blocked=0, false=0,
             leverage=100.0, verify_pass=100.0, coverage=100.0, per_completed=0.5,
             block_reasons=None):
    return {
        "attempted": attempted, "completed": completed, "blocked": blocked,
        "agentic_leverage_pct": leverage, "verify_pass_pct": verify_pass,
        "verdicts": {"true": completed, "false": false,
                     "judged": completed + false, "none_required": 0},
        "block_reasons": block_reasons or {},
        "economics": {"usage_coverage_pct": coverage,
                      "notional_cost_per_completed_usd": per_completed},
    }


class BrokenChain(unittest.TestCase):
    def test_refuses_and_posts_error(self):
        p = dd.summarize({"chain_ok": False, "error": "hash mismatch at line 9"},
                         WED)
        self.assertTrue(p["post"])
        self.assertEqual(p["severity"], "error")
        self.assertIn("REFUSED", p["title"])
        self.assertIn("line 9", p["message"])


class QuietDays(unittest.TestCase):
    def test_quiet_non_heartbeat_day_is_suppressed(self):
        p = dd.summarize(_res(records=0, note=True), WED)  # Wed, hb=Mon
        self.assertFalse(p["post"])

    def test_quiet_heartbeat_day_posts_alive_signal(self):
        p = dd.summarize(_res(records=0, note=True), MON)  # Mon == heartbeat
        self.assertTrue(p["post"])
        self.assertEqual(p["severity"], "info")
        self.assertTrue(p["meta"]["heartbeat"])

    def test_configurable_heartbeat_weekday(self):
        # With heartbeat on Wednesday, the Wed quiet day now posts.
        p = dd.summarize(_res(records=0, note=True), WED, heartbeat_weekday=2)
        self.assertTrue(p["post"])


class ActiveDay(unittest.TestCase):
    def test_clean_day_is_info_and_nominal(self):
        p = dd.summarize(_res(metrics=_metrics(), drift=[]), WED)
        self.assertTrue(p["post"])
        self.assertEqual(p["severity"], "info")
        self.assertIn("nominal", p["title"])

    def test_drift_makes_it_warn_and_leads_the_body(self):
        drift = [{"metric": "verify_pass_pct",
                  "message": "verify-pass rate fell 20pp (100% -> 80%)"}]
        p = dd.summarize(_res(metrics=_metrics(verify_pass=80.0), drift=drift), WED)
        self.assertEqual(p["severity"], "warn")
        self.assertIn("needs a look", p["title"])
        # Anomaly-forward: the drift line appears BEFORE the activity summary.
        self.assertLess(p["message"].index("drift"),
                        p["message"].index("activity"))

    def test_blocks_and_change_failures_raise_severity(self):
        m = _metrics(blocked=2, false=1,
                     block_reasons={"pii": {"count": 2}})
        p = dd.summarize(_res(metrics=m, drift=[]), WED)
        self.assertEqual(p["severity"], "warn")
        self.assertIn("blocked: 2", p["message"])
        self.assertIn("pii×2", p["message"])
        self.assertIn("change-failures (verdict=false): 1", p["message"])

    def test_undefined_metric_reads_na_not_none(self):
        # No verdicts to compute a verify-pass rate over -> "n/a", never "None%".
        p = dd.summarize(_res(metrics=_metrics(verify_pass=None), drift=[]), WED)
        self.assertIn("verify-pass: n/a", p["message"])
        self.assertNotIn("None%", p["message"])

    def test_coverage_is_stated_before_the_cost_total(self):
        p = dd.summarize(_res(metrics=_metrics(coverage=40.0, per_completed=1.25),
                              drift=[]), WED)
        # "notional", never "spend"; and coverage precedes the dollar figure.
        self.assertIn("notional", p["message"])
        self.assertNotIn("spend", p["message"].lower())
        line = next(l for l in p["message"].splitlines() if "coverage" in l)
        self.assertLess(line.index("notional"), line.index("coverage"))  # label first
        self.assertIn("40.0", line)


class NeverRaises(unittest.TestCase):
    def test_build_on_missing_ledger_does_not_crash(self):
        p = dd.build("/no/such/ledger.jsonl", days=1, scope=None,
                     heartbeat_weekday=0,
                     now=datetime.datetime(2026, 7, 22, tzinfo=datetime.timezone.utc))
        self.assertIn("post", p)          # returned a payload, did not raise
        self.assertFalse(p["post"])       # Wed quiet -> suppressed

    def test_garbage_result_becomes_an_error_post_not_an_exception(self):
        # summarize expects dict-shaped fields; a malformed metrics blob must be
        # caught by build()'s guard and surfaced, never raised.
        class Boom(dict):
            def get(self, *a, **k):
                raise RuntimeError("exploding result")
        # Drive build()'s except path by monkeypatching analyze to explode.
        orig = dd.eval_metrics.analyze
        try:
            dd.eval_metrics.analyze = lambda *a, **k: Boom()
            # point at this file so os.path.exists is True and analyze() is called
            p = dd.build(os.path.abspath(__file__), days=1, scope=None,
                         heartbeat_weekday=0)
        finally:
            dd.eval_metrics.analyze = orig
        self.assertTrue(p["post"])
        self.assertEqual(p["severity"], "error")
        self.assertIn("FAILED", p["title"])


if __name__ == "__main__":
    unittest.main()
