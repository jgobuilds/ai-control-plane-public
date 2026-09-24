#!/usr/bin/env python3
"""Resolve WHICH scope tree to use, and where its generated artifacts may go.

THE PROBLEM THIS EXISTS FOR. `context/scopes.json` is tracked, published in the
public snapshot, and rendered into `usecase-register.html`, which is on the
GitHub Pages site. Five artifacts derive from it and every one is published:

    compose.scopes.yml · context/runner-map.json · docs/usecase-register.md
    usecase-register.html (LIVE SITE) · docs/compliance-map.md

So a real scope tree in that file publishes a client list plus a sensitivity
ranking — which engagements are siloed, which block PII. That is worse than a
client list on its own: it says which clients are the sensitive ones.

THE SPLIT, matching how brand tokens already work:

    engine  context/scopes.json          neutral SAMPLE — committed, published
    overlay <overlay>/context/scopes.json REAL tree — private, never published

When the overlay tree is active, generated artifacts are written to
`<overlay>/generated/` instead of into the engine, so the engine's committed
copies stay sample-derived and publishable. The generators do not get to decide
this per-run; it follows from which tree they read.

    python scripts/scope_source.py          # say which tree is active and why

Pure stdlib.
"""
from __future__ import annotations
import glob, os, sys

ENGINE_REL = os.path.join("context", "scopes.json")


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_overlay(root: str | None = None) -> str | None:
    """$AICP_INSTANCE, else a `*-instance` sibling. Same rule as the hygiene gate —
    duplicated deliberately: this must work when that script is absent (a fresh
    clone of the engine alone), and the rule is three lines."""
    env = os.environ.get("AICP_INSTANCE")
    if env and os.path.isdir(env):
        return env
    parent = os.path.dirname(root or repo_root())
    for d in sorted(glob.glob(os.path.join(parent, "*-instance"))):
        if os.path.isdir(d):
            return d
    return None


def resolve(root: str | None = None) -> dict:
    """Which scope tree is active, and where its outputs belong.

    Returns: {path, source: "overlay"|"engine", overlay, out_dir, publishable}

    `publishable` is the field that matters. False means the artifacts generated
    from this tree MUST NOT be committed to the engine, and the caller is
    expected to honour it rather than ask again.
    """
    root = root or repo_root()
    overlay = find_overlay(root)
    if overlay:
        cand = os.path.join(overlay, ENGINE_REL)
        if os.path.isfile(cand):
            return {
                "path": cand, "source": "overlay", "overlay": overlay,
                "out_dir": os.path.join(overlay, "generated"),
                "publishable": False,
            }
    return {
        "path": os.path.join(root, ENGINE_REL), "source": "engine", "overlay": overlay,
        "out_dir": root, "publishable": True,
    }


def describe(r: dict) -> str:
    if r["source"] == "overlay":
        return (f"  scope tree: OVERLAY  {r['path']}\n"
                f"  outputs ->  {r['out_dir']}  (NOT the engine — this tree is private)\n"
                f"  publishable: NO")
    extra = ("" if r["overlay"] is None else
             f"\n  (an overlay exists at {r['overlay']} but has no context/scopes.json)")
    return (f"  scope tree: ENGINE SAMPLE  {r['path']}\n"
            f"  outputs ->  {r['out_dir']}\n"
            f"  publishable: yes — this tree is illustrative{extra}")


if __name__ == "__main__":
    r = resolve()
    print(describe(r))
    sys.exit(0)
