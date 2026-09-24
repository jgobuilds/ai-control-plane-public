#!/usr/bin/env python3
"""Prove dependabot_check fails a missing or short cooldown, and still fails a bad path.

WHY THIS EXISTS. Every Dependabot entry must declare `cooldown: { default-days: N }`
with N >= 7, the minimum release age the pins are chosen by. The failure is silent:
an entry without it falls back to Dependabot's 3-day default, proposes releases the
policy would not take, and nothing says so. When the rule was added on 2026-09-17,
the script that inserted the cooldowns missed one entry — github-actions has no
`open-pull-requests-limit` line to anchor on — and the new check caught it on its
first run. This test keeps that ability honest by synthesising each shape:

    no cooldown                 -> FAIL, names the entry and the 3-day fallback
    cooldown below 7 days       -> FAIL
    cooldown not an integer     -> FAIL
    7-day cooldown, real path   -> pass
    7-day cooldown, wrong path  -> FAIL (the original manifest check still works)

    python tests/dependabot_test.py
"""
import os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "scripts", "dependabot_check.py")
fails = []


def expect(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def run(entry_yaml, make_dir="svc"):
    tmp = tempfile.mkdtemp(prefix="depbot-")
    try:
        os.makedirs(os.path.join(tmp, "scripts"))
        os.makedirs(os.path.join(tmp, ".github"))
        os.makedirs(os.path.join(tmp, make_dir))
        open(os.path.join(tmp, make_dir, "package.json"), "w").write("{}\n")
        shutil.copy(GATE, os.path.join(tmp, "scripts", "dependabot_check.py"))
        open(os.path.join(tmp, ".github", "dependabot.yml"), "w", encoding="utf-8").write(
            "version: 2\nupdates:\n" + entry_yaml)
        p = subprocess.run([sys.executable, os.path.join("scripts", "dependabot_check.py")], cwd=tmp,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        return p.returncode, p.stdout + p.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def entry(cooldown_line, directory="/svc"):
    return (f"  - package-ecosystem: npm\n    directory: \"{directory}\"\n"
            f"    schedule: {{ interval: daily }}\n{cooldown_line}")


def main():
    print("Every Dependabot entry declares a cooldown of at least 7 days:")
    code, out = run(entry(""))
    expect("no cooldown fails", code != 0 and "3-day default" in out, out[-300:])

    code, out = run(entry("    cooldown: { default-days: 3 }\n"))
    expect("a 3-day cooldown fails", code != 0 and "below" in out, out[-300:])

    code, out = run(entry("    cooldown: { default-days: \"7\" }\n"))
    expect("a non-integer cooldown fails", code != 0, out[-300:])

    code, out = run(entry("    cooldown: { default-days: 7 }\n"))
    expect("a 7-day cooldown on a real manifest passes", code == 0, out[-300:])

    code, out = run(entry("    cooldown: { default-days: 14 }\n"))
    expect("a longer cooldown passes", code == 0, out[-300:])

    print("\nThe original manifest check still works:")
    code, out = run(entry("    cooldown: { default-days: 7 }\n", directory="/nowhere"))
    expect("a directory with no manifest still fails", code != 0 and "no package.json" in out, out[-300:])

    print()
    if fails:
        print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
        return 1
    print("All dependabot checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
