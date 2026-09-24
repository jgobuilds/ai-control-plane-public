#!/usr/bin/env python3
"""Assert that person-shaped fields in sample config hold OBVIOUSLY-FAKE values.

`pii-controls.md`, "Example data must be obviously fake". A realistic invented
name in shipped sample config is indistinguishable from a real one — to a
reader, to a search engine, and to the person who happens to share it.

WHAT THIS DOES NOT DO, SO NOBODY MISTAKES IT FOR SAFETY. It cannot tell a real
name from an invented one. Nothing can: `pii_scan.py` says so in its own output
("names and many addresses are not reliably detectable"). A regex that tried
would produce a false-positive class large enough to get the gate switched off,
which `pii-controls.md` also warns about.

So it checks the far easier property, and only that: **does this value announce
itself as an example?** A value that does is safe whether or not a real person
shares it. A value that does not is flagged — and the fix is to make it
self-evidently fake, not to argue about whether it happens to be a real person.

    python scripts/check_example_names.py <path> [--keys owner,author] [--json]

KNOWN BLIND SPOT, stated rather than discovered: it reads .json/.yaml/.yml only.
A sample name inside a fenced code block in a .md file is invisible to it — and
that is not hypothetical, it is how one survived the first run of this gate. The
fix was a human reading the doc. Widening to markdown prose would trade a small
miss for a large false-positive class, which `pii-controls.md` warns gets the
gate switched off; scanning only fenced blocks is the obvious next step if this
recurs.

Exit 1 on any finding. Pure stdlib.
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys

# Fields whose value names a HUMAN. Deliberately short: a long list drags in
# `name` and `title`, which are usually not people, and a gate that cries wolf
# gets disabled.
DEFAULT_KEYS = ["owner", "author", "contact", "approver", "requester", "assignee",
                "maintainer", "reviewer", "accountable"]

# A value is fine if it ANNOUNCES itself. Reserved domains are RFC 2606/5737;
# the rest are the conventions people already read as placeholders.
OBVIOUSLY_FAKE = [
    r"@example\.(com|org|net)\b",          # RFC 2606 reserved
    r"\bexample\.(com|org|net)\b",
    r"^<[^>]+>$",                          # <owner name>
    r"\{\{.*\}\}",                         # {{ owner }}
    r"^\$\{.*\}$",                         # ${OWNER}
    r"\b(sample|example|placeholder|dummy|fake|test|demo|tbd|unassigned|n/?a)\b",
    r"\b(Owner|Approver|Requester|Reviewer|Maintainer|Admin|Operator)$",  # role-as-surname
    r"^[-—–]$",                            # explicitly empty
]
FAKE_RE = re.compile("|".join(OBVIOUSLY_FAKE), re.I)

# NOT A PERSON AT ALL. A bare lowercase identifier — `coordinator`, `sre`,
# `platform-team` — names a role, service or agent, and no human name looks like
# that in a config file. Skipping these is what keeps the false-positive rate
# low enough that the gate survives: `pii-controls.md` warns that a noisy
# placeholder check gets switched off, and a check that is off protects nothing.
#
# A human name in one of these fields has a space or is an address, so this
# exemption cannot swallow the case the gate exists for.
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-/]*$")

SKIP_DIRS = {".git", "node_modules", "__pycache__", "vault", "audit", "generated",
             ".venv", "venv", "dist", "build"}


def tracked_files(root):
    """Only what git tracks — untracked scratch is not shipped, so not in scope."""
    try:
        r = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0 and r.stdout.strip():
            return [os.path.join(root, *p.split("/")) for p in r.stdout.split("\n") if p.strip()]
    except (OSError, subprocess.SubprocessError):
        pass
    out = []
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        out += [os.path.join(dirpath, n) for n in names]
    return out


def walk_json(obj, keys, path=""):
    """Yield (dotted_path, key, value) for every person-shaped string field."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            here = f"{path}.{k}" if path else k
            if isinstance(v, str) and k.lower() in keys and v.strip():
                yield here, k, v
            else:
                yield from walk_json(v, keys, here)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_json(v, keys, f"{path}[{i}]")


def check_path(root, keys):
    findings = []
    for p in tracked_files(root):
        if not p.endswith((".json", ".yaml", ".yml")):
            continue
        if any(s in p.replace("\\", "/").split("/") for s in SKIP_DIRS):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                raw = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        if p.endswith(".json"):
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            pairs = list(walk_json(data, keys))
        else:
            # No yaml dependency: match `key: value` textually. Coarser, and the
            # alternative is a dependency in a stdlib-only toolchain.
            pairs = []
            for i, line in enumerate(raw.splitlines(), 1):
                m = re.match(r"\s*([A-Za-z_]+)\s*:\s*[\"']?([^\"'#\n]+?)[\"']?\s*$", line)
                if m and m.group(1).lower() in keys and m.group(2).strip():
                    pairs.append((f"line {i}", m.group(1), m.group(2).strip()))
        for where, key, val in pairs:
            if FAKE_RE.search(val) or IDENTIFIER_RE.match(val.strip()):
                continue
            findings.append({
                "file": os.path.relpath(p, root).replace("\\", "/"),
                "where": where, "key": key, "value": val,
            })
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--keys", default=",".join(DEFAULT_KEYS))
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.path)
    keys = {k.strip().lower() for k in a.keys.split(",") if k.strip()}

    findings = check_path(root, keys)
    if a.as_json:
        print(json.dumps(findings, indent=2))
        return 1 if findings else 0

    print(f"Example-name check — person-shaped fields: {', '.join(sorted(keys))}")
    if not findings:
        print("  every sample value announces itself as an example. ✓")
        print("  NOTE: this cannot detect a REAL name, and does not try. It checks that a\n"
              "        value is obviously fake — which is the property you can actually gate.")
        return 0
    for f in findings:
        # Print the VALUE here on purpose: it is asserted to be example data, and
        # the operator cannot fix what they cannot see. `pii_scan.py`'s
        # never-print rule applies to suspected REAL data; this is the inverse.
        print(f"  {f['file']} [{f['where']}] {f['key']} = {f['value']!r}")
    print(f"\n  {len(findings)} value(s) do not announce themselves as examples.\n"
          "  Make the fakeness self-evident IN THE VALUE — an @example.com address,\n"
          "  a <placeholder>, or a role-as-name like 'Dana Owner'. A banner elsewhere\n"
          "  saying 'this is sample data' does not fix this: banners get skimmed and\n"
          "  cropped out of screenshots; values get quoted.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
