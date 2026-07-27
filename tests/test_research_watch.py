#!/usr/bin/env python3
"""Labeled corpus for the research-watch signal detector.

This test exists because the detector was shipped WITHOUT it and was wrong within
a minute: `"rce"` was matched as a substring, and since GitHub release bodies are
HTML, every single release contained "source" or "resource" and therefore looked
like a remote-code-execution advisory. change-safety.md already says it — "a
heuristic without a labeled corpus is unshipped" — and this is the corpus.

Every false positive found in the wild is a permanent case here.

    python -m unittest tests.test_research_watch
"""
import os, importlib.util, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "research_watch", os.path.join(HERE, "..", "scripts", "research_watch.py"))
rw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rw)


# Text that MUST produce no signal. Every entry is real release-note noise.
MUST_STAY_SILENT = [
    ('<source media="(prefers-color-scheme: dark)" srcset="a.svg">', "html <source> tag"),
    ("Improved resource cleanup on shutdown", "the word 'resource'"),
    ("Refactored the data source connector", "the word 'source'"),
    ("Open in stagereview.app for a preview", "vendor link boilerplate"),
    ("chore: bump dependencies", "an ordinary chore release"),
    ("feat: add a new node for outsourcing tasks", "'outsourcing' contains rce"),
    ("Fixed a typo in the security-scanner docs page title", "'security' as a word IS a signal"),
]

# Text that MUST produce the named signal.
MUST_CATCH = [
    ("Fixes an RCE in the task runner sandbox", "rce"),
    ("Addresses remote code execution via workflow expressions", "rce"),
    ("Patches CVE-2026-21858", "cve"),
    ("This is a BREAKING CHANGE for the API", "breaking"),
    ("The v1 endpoint is deprecated and will be removed", "deprecation"),
    ("Resolves an authentication bypass in the proxy", "auth-bypass"),
    ("Fixes a vulnerability in the parser", "vulnerability"),
    ("Announcing a licence change to BSL", "license"),
    ("Node 18 reaches end-of-life this month", "eol"),
]


class NoFalsePositives(unittest.TestCase):
    def test_release_note_noise_is_silent(self):
        for text, why in MUST_STAY_SILENT:
            if why == "'security' as a word IS a signal":
                continue          # handled explicitly below — it SHOULD fire
            with self.subTest(why=why):
                self.assertEqual(rw.signals_in(text), [],
                                 f"false positive on {why}: {text!r}")

    def test_the_original_bug_stays_fixed(self):
        # The exact string that made every n8n release look like an advisory.
        body = '<source media="(prefers-color-scheme: dark)" srcset="x.svg"><a href="h">p</a>'
        self.assertNotIn("rce", rw.signals_in(body))

    def test_security_as_a_real_word_still_fires(self):
        # Guard against over-correcting: the fix must not silence true positives.
        self.assertIn("security", rw.signals_in("This is a security release"))


class CatchesRealSignals(unittest.TestCase):
    def test_each_signal_fires_on_its_phrase(self):
        for text, expected in MUST_CATCH:
            with self.subTest(signal=expected):
                self.assertIn(expected, rw.signals_in(text),
                              f"missed {expected} in {text!r}")


class MarkupHandling(unittest.TestCase):
    def test_markup_is_stripped_before_matching(self):
        self.assertNotIn("<", rw.strip_markup("<b>hello</b>"))

    def test_urls_are_stripped(self):
        # A URL can contain almost any substring; matching inside one is noise.
        self.assertNotIn("rce", rw.signals_in("see https://example.com/rce-details"))


class RunningVsWatched(unittest.TestCase):
    """We RUN some of these and merely WATCH others as candidates. A release in
    something we run is an operational event; the same release in a deferred
    candidate is not. Conflating them is how a report overstates its own urgency."""

    def test_running_is_a_subset_of_watch(self):
        # A typo here would silently reclassify a running dependency as
        # "candidate we don't run" — quietly downgrading its releases.
        self.assertEqual(rw.RUNNING - set(rw.WATCH), set(),
                         "RUNNING names a repo that is not in WATCH")

    def test_every_pinned_repo_is_marked_running(self):
        # If we pin its image in image-policy.json, we run it by definition.
        self.assertEqual(set(rw.PINNED_AS) - rw.RUNNING, set(),
                         "a repo we pin an image for is not marked as running")


class HonestEmptyResult(unittest.TestCase):
    """A failed fetch must never render as 'nothing changed'."""

    def setUp(self):
        self._real = rw.fetch

    def tearDown(self):
        rw.fetch = self._real

    def test_a_failed_source_lands_in_errors_not_in_silence(self):
        rw.fetch = lambda repo, timeout=15: (None, "URLError")
        items, errors = rw.analyse(30)
        self.assertEqual(items, [], "no items should be produced when every fetch failed")
        self.assertEqual(len(errors), len(rw.WATCH),
                         "every unreachable source must be reported, not dropped")

    def test_one_dead_source_does_not_kill_the_others(self):
        real = self._real
        def flaky(repo, timeout=15):
            return (None, "HTTP 404") if repo == list(rw.WATCH)[0] else ([], None)
        rw.fetch = flaky
        items, errors = rw.analyse(30)
        self.assertEqual(len(errors), 1, "only the dead source should error")


if __name__ == "__main__":
    unittest.main()
