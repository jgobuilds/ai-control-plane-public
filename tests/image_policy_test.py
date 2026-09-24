#!/usr/bin/env python3
"""Prove image_policy_check fails a pinned ref that does not name its policy tag.

WHY THIS EXISTS. On 2026-09-03 a Dependabot base-image bump was accepted whose
digests were `node:latest` and `python:latest` — Node 26 and Python 3.14 on full
Debian 13 — while the policy said node:22-bookworm-slim and python:3.12-alpine.
The FROM lines were `node@sha256:...` with no tag, which is exactly why Dependabot
proposed :latest: it had nothing else to track. Every gate passed. The security
scan went red ten days later, on CVEs in packages a slim image never contains.

The fix is to pin `image:tag@sha256:...`. This test synthesises the failure and
demands the gate go red, because a check written after the fact passes on the
fixed tree whether or not it can see the bug:

    digest-only ref (the 2026-09-03 shape)   -> FAIL, and the message names :latest
    wrong tag                                -> FAIL
    right tag, digest not the policy's       -> FAIL
    right tag and digest                     -> pass
    a registry port is not read as a tag     -> pass

It CANNOT test that a digest belongs to its tag — neither can the gate; that needs
a registry. It proves the static half, which is the half that runs in CI.

    python tests/image_policy_test.py
"""
import json, os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "scripts", "image_policy_check.py")
D1 = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64

fails = []


def expect(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def run(from_line, images):
    """A throwaway repo with one Dockerfile and one policy, and the real gate copied in."""
    tmp = tempfile.mkdtemp(prefix="imgpol-")
    try:
        os.makedirs(os.path.join(tmp, "scripts"))
        os.makedirs(os.path.join(tmp, "context"))
        os.makedirs(os.path.join(tmp, "svc"))
        shutil.copy(GATE, os.path.join(tmp, "scripts", "image_policy_check.py"))
        open(os.path.join(tmp, "svc", "Dockerfile"), "w", encoding="utf-8").write(
            f"# a comment line above, never trailing\n{from_line}\n")
        json.dump({"reviewBy": "2999-01-01", "images": images},
                  open(os.path.join(tmp, "context", "image-policy.json"), "w", encoding="utf-8"))
        p = subprocess.run([sys.executable, os.path.join("scripts", "image_policy_check.py")], cwd=tmp,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        return p.returncode, p.stdout + p.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    node = [{"ref": "node", "tag": "22-bookworm-slim", "digest": D1}]

    print("A pinned ref must name the tag its policy records:")
    code, out = run(f"FROM node@{D1}", node)
    expect("digest-only ref fails (the 2026-09-03 shape)", code != 0, out[-300:])
    expect("...and the message says Dependabot will follow :latest", "node:latest" in out, out[-300:])

    code, out = run(f"FROM node:latest@{D1}", node)
    expect("wrong tag fails", code != 0 and "does not match" in out, out[-300:])

    code2, out2 = run(f"FROM node:22-bookworm-slim@{D2}", node)
    expect("right tag with a digest the policy does not record fails", code2 != 0
           and "uses the digest the policy records" in out2, out2[-300:])

    code, out = run(f"FROM node:22-bookworm-slim@{D1}", node)
    expect("right tag and digest passes", code == 0, out[-400:])

    code, out = run(f"FROM registry.local:5000/node:22-bookworm-slim@{D1}",
                    [{"ref": "registry.local:5000/node", "tag": "22-bookworm-slim", "digest": D1}])
    expect("a registry port is not mistaken for a tag", code == 0, out[-400:])

    code, out = run(f"FROM node:22-bookworm-slim@{D1} AS build", node)
    expect("a multi-stage `AS name` suffix does not confuse the parse", code == 0, out[-400:])

    print()
    if fails:
        print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
        return 1
    print("All image-policy checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
