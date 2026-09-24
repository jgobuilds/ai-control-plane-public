#!/usr/bin/env python3
"""The engine must never carry a REAL scope tree, or anything derived from one.

`context/scopes.json` is tracked, published in the public snapshot, and rendered
into `usecase-register.html`, which is served from the GitHub Pages site. Five
artifacts derive from it and every one is published:

    compose.scopes.yml · context/runner-map.json · docs/usecase-register.md
    usecase-register.html (LIVE SITE) · docs/compliance-map.md

A real tree there publishes a client list AND a sensitivity ranking — which
engagements are siloed, which block PII. The second half is worse than the
first: it says which clients are the sensitive ones.

This asserts the SPLIT holds, not that any particular name is absent. It cannot
know a real client name and does not guess: it checks that the engine's tree is
the declared sample, and that every scope name in a published artifact comes
from that sample. A name that appears in a published file and NOT in the sample
tree has leaked in from somewhere, which is the failure this catches.

    python tests/scope_privacy_test.py
"""
import importlib.util, io, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "scope_source", os.path.join(ROOT, "scripts", "scope_source.py"))
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)

DERIVED = ["compose.scopes.yml", "context/runner-map.json",
           "docs/usecase-register.md", "usecase-register.html"]

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


engine_tree = os.path.join(ROOT, "context", "scopes.json")
data = json.load(io.open(engine_tree, encoding="utf-8"))
names = set(data.get("nodes", {}))

print("The engine's committed tree declares itself a sample:")
# Without this marker a real tree could be committed and nothing would notice.
check("context/scopes.json carries a _sample_data declaration",
      "_sample_data" in data,
      "add it, or this file is indistinguishable from a real tree")
check("its owners are @example.com, not people",
      all("@example.com" in (n.get("owner") or "owner@example.com")
          for n in data["nodes"].values()),
      "run scripts/check_example_names.py")

print("\nThe resolver sends a real tree somewhere private:")
r = ss.resolve(ROOT)
check("engine-sample mode is publishable", ss.resolve(ROOT)["publishable"] is True
      if r["source"] == "engine" else True)
# Simulate an overlay tree without creating one, by pointing AICP_INSTANCE at a
# temp dir. The property under test is the ROUTING of outputs, not the content.
import tempfile
tmp = tempfile.mkdtemp()
os.makedirs(os.path.join(tmp, "context"), exist_ok=True)
io.open(os.path.join(tmp, "context", "scopes.json"), "w", encoding="utf-8").write('{"nodes":{}}')
old = os.environ.get("AICP_INSTANCE")
os.environ["AICP_INSTANCE"] = tmp
try:
    ro = ss.resolve(ROOT)
    check("an overlay tree is detected", ro["source"] == "overlay", ro["source"])
    check("an overlay tree is NOT publishable", ro["publishable"] is False)
    check("its outputs are written to the overlay, not the engine",
          os.path.commonpath([ro["out_dir"], tmp]) == tmp, ro["out_dir"])
finally:
    if old is None:
        os.environ.pop("AICP_INSTANCE", None)
    else:
        os.environ["AICP_INSTANCE"] = old

print("\nNo published artifact names a scope the sample tree does not have:")
# The leak-detector. Scope names appear as runner hostnames, SCOPE_ROOT values,
# and table cells; any that is not in the sample tree came from somewhere else.
for rel in DERIVED:
    p = os.path.join(ROOT, *rel.split("/"))
    if not os.path.isfile(p):
        continue
    body = io.open(p, encoding="utf-8", errors="replace").read()
    found = set(re.findall(r"claude-runner-([a-z0-9][a-z0-9-]*)", body))
    # SCOPE_ROOT is a PATH ("scopes/a/b"); take the LAST segment, which is the
    # scope. Matching the first segment flagged the literal "scopes" as an
    # unknown scope — a false positive introduced the moment the format changed,
    # and a leak detector that cries wolf is one people switch off.
    found |= {m.rstrip("/").split("/")[-1]
              for m in re.findall(r"SCOPE_ROOT=([A-Za-z0-9/_-]+)", body)}
    unknown = {f for f in found if f not in names and f != "commons"}
    check(f"{rel} names only sample scopes", not unknown,
          f"unknown scope name(s) {sorted(unknown)} — a real tree may have leaked in")

print("\nNo TRACKED workspace content belongs to a scope outside the sample tree:")
# The five DERIVED artifacts above were the known leak paths. `workspace/` was
# not one of them and should have been: it is tracked, it is NOT in
# .publish-exclude, and the runtime scope tree lives there. The real tree's
# directories sat in the working copy for weeks without leaking only because git
# does not track empty directories — the first CLAUDE.md written into one would
# have published the scope's name and the shape of the tree.
import subprocess
try:
    tracked = subprocess.run(["git", "ls-files", "workspace/scopes"], cwd=ROOT,
                             capture_output=True, text=True, check=True,
                             encoding="utf-8").stdout.split()
except (OSError, subprocess.CalledProcessError) as e:
    print(f"  SKIP  git unavailable ({e})")
    tracked = []
leaked = sorted({seg for p in tracked
                 for seg in p.split("/")[2:-1]            # dir segments only
                 if seg not in names})
check("tracked workspace/scopes paths name only sample scopes", not leaked,
      f"{leaked} — a real scope tree is being committed into the engine; "
      "the .gitignore allowlist under workspace/scopes/ is what keeps it out")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All scope-privacy checks passed.")
