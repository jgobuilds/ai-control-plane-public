#!/usr/bin/env python3
"""Regression corpus for workspace_doctor's local-path guard.

The guard shipped with false positives found only by running it across the real
fleet: container paths (/home/node/), and a garbage drive-letter match
(claude-config:/home matching as "g:/home"). Each is a permanent case here now,
alongside the real leaks it must still catch.

    python -m unittest tests.test_workspace_doctor
"""
import os, re, importlib.util, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WD_PATH = os.path.join(HERE, "..", "scripts", "workspace_doctor.py")
_spec = importlib.util.spec_from_file_location("workspace_doctor", WD_PATH)
wd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wd)


def flags(line):
    """True if any local-path pattern matches the line."""
    return any(re.search(pat, line) for pat, _ in wd.LOCAL_PATH_PATTERNS)


class LocalPathGuard(unittest.TestCase):
    MUST_FLAG = [
        ("windows dev drive",   r"entry: python D:\code\ai-standards\pii_scan.py"),
        ("windows fwd-slash",   "entry: python D:/code/ai-standards/pii_scan.py"),
        ("windows users",       r"$gh = 'C:\Users\goldb\tool.exe'"),
        ("developer unix home", "run: python /home/goldb/tool.py"),
        ("macos home",          "path = /Users/goldb/project/x"),
        ("wsl mount",           "cd /mnt/c/code/thing"),
        ("wsl drive",           "source /d/code/env/activate"),
    ]
    MUST_NOT_FLAG = [
        # container/service filesystem — identical on every machine, portable
        ("container node home", "mkdir -p /home/node/.claude /workspace"),
        ("compose volume mount", "- claude-config:/home/node/.claude"),
        ("container app home",  "WORKDIR /home/app"),
        ("root home",           "cp x /root/.config/y"),
        # garbage match the guard produced once: the 'g' of 'config' + ':/home'
        ("word-then-colon",     "some-config:/home/node/x"),
        # ordinary relative paths — the portable, correct form
        ("relative overlay",    "overlay: ../ai-standards"),
        ("relative script",     "entry: python scripts/pii_scan.py"),
        ("url not a path",      "url: https://github.com/jgobuilds/x"),
    ]

    def test_must_flag(self):
        for label, line in self.MUST_FLAG:
            with self.subTest(label):
                self.assertTrue(flags(line), f"{label!r} should be flagged: {line!r}")

    def test_must_not_flag(self):
        for label, line in self.MUST_NOT_FLAG:
            with self.subTest(label):
                self.assertFalse(flags(line), f"{label!r} is a false positive: {line!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
