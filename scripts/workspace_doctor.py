#!/usr/bin/env python3
"""Workspace doctor — designs and audits an agent context layout.

Deterministic (Tier 0): every rule here is a file-shape fact, so this runs for
free, in CI, with no model call. That is the point — the layout standard in
AGENT-WORKSPACE.md is only real if something checks it.

Two modes, same rules:
  audit  — grade an existing tree, ranked findings          (default)
  plan   — propose the layout for a tree that has none yet

    python scripts/workspace_doctor.py [PATH]
    python scripts/workspace_doctor.py [PATH] --plan
    python scripts/workspace_doctor.py [PATH] --json     # for CI / conformance

Exit codes: 0 clean, 1 findings at or above --fail-on (default "high"), 2 error.
"""
import os, sys, json, re, argparse, hashlib

# Budgets are documented limits, not taste. See AGENT-WORKSPACE.md §4.
CLAUDE_MD_LINES   = 200      # target; longer measurably reduces adherence
SKILL_MD_LINES    = 500      # push detail into reference.md / examples.md
MEMORY_MD_LINES   = 200      # first 200 lines OR 25KB, whichever comes first
MEMORY_MD_BYTES   = 25 * 1024
TINY_NESTED_LINES = 5        # below this, a nested file earns its keep poorly

# A directory is a "package" (deserves its own context) if it carries one of
# these markers — the same signal a human uses to say "this is a thing".
STACK_MARKERS = {
    "package.json": "node", "pyproject.toml": "python", "setup.py": "python",
    "go.mod": "go", "Cargo.toml": "rust", "pom.xml": "java",
    "build.gradle": "java", "Gemfile": "ruby", "composer.json": "php",
    "requirements.txt": "python", "Dockerfile": "container",
}
SPLIT_MIN_FILES = 8          # a package this size is worth local context
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
             "build", ".next", "target", "vendor", ".pytest_cache", ".mypy_cache",
             "site-packages", ".terraform", "coverage", ".tox"}
SOURCE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb",
              ".php", ".cs", ".sh", ".sql", ".yml", ".yaml", ".tf"}

# Literal-secret shapes. Deliberately narrow: a false positive here costs a
# human a minute, a false negative commits a live credential to every session.
SECRET_PATTERNS = [
    (r"sk-ant-[A-Za-z0-9_\-]{20,}",              "Anthropic key/token"),
    (r"gh[pousr]_[A-Za-z0-9]{20,}",              "GitHub token"),
    (r"AKIA[0-9A-Z]{16}",                        "AWS access key id"),
    (r"AIza[0-9A-Za-z_\-]{30,}",                 "Google API key"),
    (r"xox[baprs]-[0-9A-Za-z\-]{10,}",           "Slack token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----",      "private key"),
    (r"(?i)(api[_-]?key|secret|passwd|password|token)\s*[:=]\s*"
     r"['\"][A-Za-z0-9_\-/+]{16,}['\"]",         "hardcoded credential"),
]
# Derivable content: things the agent can just look up. Costs tokens every
# session and goes stale silently. /doctor trims exactly these.
#
# Each rule needs MIN consecutive-ish hits before it fires. A single line that
# looks tree-ish is almost always an inline path in prose; three of them is a
# tree. Requiring box-drawing/ASCII tree connectors specifically (not backticks
# or bare hyphens) keeps `path/to/file` in a sentence from tripping it.
DERIVABLE_PATTERNS = [
    (r"(?m)^\s*(?:│|├──|└──|\|--|\+--|`--)", "directory tree", 3),
    (r"(?im)^\s*[-*]\s+[a-z0-9_.\-]+\s*(?:==|>=|<=|~=|\^)\s*v?\d+\.\d+", "dependency list", 3),
]

findings = []   # (severity, kind, path, message, action)
SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def add(sev, kind, path, message, action):
    findings.append((sev, kind, path, message, action))


def read(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def nonempty_lines(text):
    return [l for l in text.splitlines() if l.strip()]


def rel(root, p):
    try:
        return os.path.relpath(p, root).replace("\\", "/")
    except ValueError:
        return p.replace("\\", "/")


def walk(root):
    """Yield (dirpath, dirnames, filenames), pruning noise directories."""
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS and not d.startswith(".git")]
        yield dp, dn, fn


# ---------------------------------------------------------------- checks

def check_instruction_files(root):
    """Size + content of every CLAUDE.md / AGENTS.md in the tree, plus the
    ancestor chain above it (ancestors load eagerly into every session)."""
    seen = []
    # Ancestors: everything above root is loaded eagerly, so it counts.
    cur = os.path.dirname(os.path.abspath(root))
    while True:
        for name in ("CLAUDE.md", "AGENTS.md"):
            p = os.path.join(cur, name)
            if os.path.isfile(p):
                seen.append((p, True))
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    for dp, _, fn in walk(root):
        for name in ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md"):
            if name in fn:
                seen.append((os.path.join(dp, name), False))

    for p, is_ancestor in seen:
        text = read(p)
        lines = nonempty_lines(text)
        r = rel(root, p)
        where = "ancestor — loaded into EVERY session" if is_ancestor else "in-tree"
        if len(lines) > CLAUDE_MD_LINES:
            add("high" if is_ancestor else "medium", "budget", r,
                f"{len(lines)} lines (target <{CLAUDE_MD_LINES}); {where}",
                "Move sometimes-needed detail into a skill; keep only always-true rules.")
        for pat, label, minimum in DERIVABLE_PATTERNS:
            hits = len(re.findall(pat, text))
            if label == "directory tree":
                # A tree whose lines carry arrows is a *relationship diagram*
                # (X points at Y), not an inventory of what exists. The agent
                # can derive the latter by looking; it cannot derive the former.
                arrowed = len(re.findall(
                    r"(?m)^\s*(?:│|├──|└──|\|--|\+--|`--).*(?:->|<-|→|←)", text))
                hits -= arrowed
            if hits >= minimum:
                add("medium", "derivable", r,
                    f"contains a {label} ({hits} lines) the agent can derive from the tree",
                    "Delete it. Keep rationale and gotchas, not inventories.")
                break
        for pat, label in SECRET_PATTERNS:
            if re.search(pat, text):
                add("high", "secret", r,
                    f"looks like a literal {label} in an always-loaded file",
                    "Remove it. Context files get no scrubbing — this loads verbatim every session.")
                break
        if (not is_ancestor and os.path.basename(p) == "CLAUDE.md"
                and os.path.dirname(os.path.abspath(p)) != os.path.abspath(root)
                and 0 < len(lines) < TINY_NESTED_LINES):
            add("low", "merge", r,
                f"only {len(lines)} lines — a nested file this small rarely earns its place",
                "Fold into the parent instruction file, or grow it to carry real local rules.")
    return seen


def check_root_context(root, seen, portable):
    """A repo with no instruction file of its own inherits whatever happens to
    sit above it on THIS machine — and nothing at all once someone else clones
    it. For a repo with outside contributors that is the finding that matters
    most, and it is invisible to every other check here."""
    has_root = any(os.path.dirname(os.path.abspath(p)) == os.path.abspath(root)
                   for p, anc in seen if not anc)
    ancestors = [p for p, anc in seen if anc]
    if has_root:
        return
    if portable:
        add("high", "portability", ".",
            "no CLAUDE.md/AGENTS.md at the repo root, and this repo is marked portable",
            "A cloner gets ZERO conventions — ancestor files do not travel. Commit a "
            "root instruction file carrying everything the repo needs, self-contained.")
    else:
        add("medium", "context", ".",
            "no CLAUDE.md/AGENTS.md at the repo root"
            + (f"; currently inheriting {len(ancestors)} ancestor file(s) from outside the repo"
               if ancestors else "; nothing is providing conventions"),
            "Add one. Inherited-only context works on this machine and nowhere else.")


# Machine-specific absolute paths. Any of these in a COMMITTED file breaks for
# anyone else — and in a public repo, leaks the author's directory layout. This
# is the check that would have caught a private D:/code/ai-standards path being
# written into a public repo's .pre-commit-config.yaml.
# Container/service home dirs are IDENTICAL on every machine (the image's own
# filesystem), so they are portable and must not be flagged. Only a real
# developer's home directory is the leak.
_CONTAINER_USERS = r"(?:node|app|runner|root|ubuntu|user|www-data|nobody|appuser)"
LOCAL_PATH_PATTERNS = [
    # A real Windows drive path. (?<![\w]) stops "claude-config:/home" from
    # matching (the 'g' of 'config' is not a drive letter).
    (r"(?<![\w])[A-Za-z]:[\\/](?:Users|code|home)\b", "Windows absolute path"),
    # A developer home, but NOT a container service account (/home/node/ etc).
    (r"/(?:home|Users)/(?!" + _CONTAINER_USERS + r"/)[A-Za-z0-9._-]+/", "Unix home path"),
    (r"/mnt/[a-z]/", "WSL mount path"),
    (r"(?<![\w])/[a-z]/code/", "WSL drive path"),
]
# Files most likely to carry a load-bearing path (config/CI/hooks). Instruction
# files are already covered by their own link check.
PATHY_NAMES = (".pre-commit-config.yaml", ".mcp.json", "config.yml", "config.yaml")
PATHY_DIRS = (".github", ".engineering-standard")


def check_local_paths(root):
    """Scan tracked (or, outside git, all) config/CI files for absolute local
    paths. Runs for EVERY repo, not just portable ones — a hard-coded home
    directory is a latent portability bug regardless of who clones it."""
    tracked = tracked_files(root)
    if tracked is None:  # not a git repo — walk the tree
        tracked = [os.path.join(dp, f) for dp, _, fn in walk(root) for f in fn]
    for p in tracked:
        base = os.path.basename(p)
        rp = rel(root, p)
        pathy = (base in PATHY_NAMES
                 or any(seg in PATHY_DIRS for seg in rp.split("/"))
                 or base.endswith((".yml", ".yaml", ".json", ".cfg", ".ini", ".sh", ".ps1")))
        if not pathy:
            continue
        text = read(p)
        for pat, label in LOCAL_PATH_PATTERNS:
            if re.search(pat, text):
                add("high", "local-path", rp,
                    f"contains a {label} — machine-specific, breaks for anyone else",
                    "Use a relative path, an env var, or fetch the dependency in CI. "
                    "A committed absolute path is a portability bug (and leaks your "
                    "directory layout in a public repo).")
                break


def tracked_files(root):
    r = git(root, "ls-files", "-z")
    if r.returncode != 0:
        return None
    return [os.path.join(root, p) for p in r.stdout.split("\0") if p]


def git(root, *args):
    import subprocess
    return subprocess.run(["git", "-C", root, *args],
                          capture_output=True, text=True, errors="replace")


def check_portability(root, portable):
    """Standards that reference private siblings by relative path break the
    moment someone clones the repo on its own."""
    if not portable:
        return
    cfg = os.path.join(root, ".engineering-standard", "config.yml")
    if os.path.isfile(cfg):
        text = read(cfg)
        m = re.search(r"(?m)^\s*overlay:\s*(\S+)", text)
        if m and m.group(1).startswith(".."):
            add("high", "portability", ".engineering-standard/config.yml",
                f"overlay points outside the repo ({m.group(1)}) on a portable repo",
                "A cloner has no such sibling. Vendor the lenses, install them as a "
                "plugin, or reference a PUBLIC standard by URL.")
    for dp, _, fn in walk(root):
        for name in ("CLAUDE.md", "AGENTS.md"):
            if name not in fn:
                continue
            p = os.path.join(dp, name)
            for link in re.findall(r"\]\((\.\./[^)]+)\)", read(p)):
                if link.startswith("../"):
                    add("medium", "portability", rel(root, p),
                        f"links outside the repo ({link})",
                        "Broken for anyone who clones this repo alone. Use a URL or inline it.")
                    break


# Cross-vendor adapter paths. AGENTS.md is the standard; these are where each
# other tool looks. A repo with outside contributors should not assume everyone
# uses the same agent.
ADAPTER_PATHS = {
    "CLAUDE.md": "Claude Code",
    ".github/copilot-instructions.md": "GitHub Copilot",
    ".cursor/rules": "Cursor",
    ".windsurfrules": "Windsurf",
    ".clinerules": "Cline",
    "GEMINI.md": "Gemini CLI",
}


def check_adapters(root, portable):
    """AGENTS.md is read natively by most agents but NOT by Claude Code. On a
    portable repo, whichever tool a contributor uses should find its own file --
    and each of those files should point at AGENTS.md rather than be a copy."""
    if not portable:
        return
    agents = os.path.join(root, "AGENTS.md")
    if not os.path.isfile(agents):
        return  # check_root_context already reports the missing-source case
    present, copies = [], []
    for relp, label in ADAPTER_PATHS.items():
        full = os.path.join(root, relp.replace("/", os.sep))
        if not os.path.exists(full):
            continue
        present.append(label)
        files = ([os.path.join(dp, f) for dp, _, fn in os.walk(full) for f in fn]
                 if os.path.isdir(full) else [full])
        for f in files:
            if "AGENTS.md" not in read(f):
                copies.append(rel(root, f))
    for c in copies:
        add("medium", "adapter", c,
            "tool instruction file does not reference AGENTS.md",
            "Looks like a divergent copy rather than a pointer. Regenerate it; "
            "copies drift silently.")
    if "Claude Code" not in present:
        add("medium", "adapter", ".",
            "AGENTS.md exists but there is no CLAUDE.md",
            "Claude Code does NOT read AGENTS.md natively. Add CLAUDE.md with an "
            "@AGENTS.md import, or Claude users get nothing.")
    if len(present) <= 1:
        add("low", "adapter", ".",
            f"AGENTS.md present, adapters for {len(present)} tool(s)",
            "Contributors may use Cursor, Copilot, Windsurf or Cline. Generate "
            "pointer files so any tool finds its own path.")


def check_agents_md_pairing(root, seen):
    """Both CLAUDE.md and AGENTS.md in one directory, with different content,
    is the duplication anti-pattern: they drift and the two tool families
    resolve conflicts differently."""
    by_dir = {}
    for p, _ in seen:
        by_dir.setdefault(os.path.dirname(p), set()).add(os.path.basename(p))
    for d, names in by_dir.items():
        if {"CLAUDE.md", "AGENTS.md"} <= names:
            a, b = read(os.path.join(d, "CLAUDE.md")), read(os.path.join(d, "AGENTS.md"))
            if a.strip() == b.strip():
                continue
            if "@AGENTS.md" in a:      # the documented import — correct
                continue
            add("medium", "duplicate", rel(root, d),
                "CLAUDE.md and AGENTS.md both present with different content",
                "Keep one source of truth; import the other with `@AGENTS.md` or symlink it.")


def check_skills_and_memory(root):
    for dp, _, fn in walk(root):
        for f in fn:
            p, r = os.path.join(dp, f), rel(root, os.path.join(dp, f))
            if f == "SKILL.md":
                n = len(nonempty_lines(read(p)))
                if n > SKILL_MD_LINES:
                    add("medium", "budget", r, f"{n} lines (target <{SKILL_MD_LINES})",
                        "Move detail into reference.md / examples.md; SKILL.md is a table of contents.")
            elif f == "MEMORY.md":
                text = read(p)
                n, b = len(text.splitlines()), len(text.encode("utf-8"))
                if n > MEMORY_MD_LINES or b > MEMORY_MD_BYTES:
                    add("high", "budget", r,
                        f"{n} lines / {b}B (limit {MEMORY_MD_LINES} lines or {MEMORY_MD_BYTES}B)",
                        "Content past the limit is SILENTLY DROPPED at load. Split into topic files.")


def check_mcp_config(root):
    for dp, _, fn in walk(root):
        if ".mcp.json" not in fn:
            continue
        p, r = os.path.join(dp, ".mcp.json"), rel(root, os.path.join(dp, ".mcp.json"))
        text = read(p)
        for pat, label in SECRET_PATTERNS:
            if re.search(pat, text):
                add("high", "secret", r, f"literal {label} in a committed config",
                    "Use ${VAR} expansion. NOTE: an unset var loads with a warning, not an error — verify explicitly.")
                break
        try:
            cfg = json.loads(text)
        except json.JSONDecodeError:
            add("medium", "config", r, "not valid JSON", "Fix the syntax; a broken .mcp.json fails quietly.")
            continue
        for name, srv in (cfg.get("mcpServers") or {}).items():
            env = srv.get("env") or {}
            for k, v in env.items():
                if isinstance(v, str) and v and "${" not in v and len(v) > 12:
                    add("medium", "secret", r,
                        f"server '{name}' env {k} has a literal value",
                        "Switch to ${VAR} so each machine supplies its own.")


def check_drift(root, seen):
    """Copy-paste drift: the same guidance living in two places diverges
    silently. Compare paragraph fingerprints across instruction files."""
    blocks = {}
    for p, _ in seen:
        for para in re.split(r"\n\s*\n", read(p)):
            s = " ".join(para.split())
            if len(s) < 120 or s.startswith("#"):
                continue
            h = hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]
            blocks.setdefault(h, (s, []))[1].append(rel(root, p))
    for h, (s, paths) in blocks.items():
        uniq = sorted(set(paths))
        if len(uniq) > 1:
            add("medium", "drift", uniq[0],
                f"identical paragraph duplicated in {len(uniq)} files: {', '.join(uniq)}",
                "One canonical copy — symlink or package it; copies diverge silently.")


def scan_packages(root):
    """Find directories that look like real packages, with their file counts."""
    pkgs = []
    for dp, _, fn in walk(root):
        marker = next((m for m in fn if m in STACK_MARKERS), None)
        if not marker or os.path.abspath(dp) == os.path.abspath(root):
            continue
        n = sum(1 for _, _, f2 in walk(dp)
                for x in f2 if os.path.splitext(x)[1] in SOURCE_EXT)
        pkgs.append({"dir": rel(root, dp), "stack": STACK_MARKERS[marker],
                     "marker": marker, "files": n,
                     "has_context": os.path.isfile(os.path.join(dp, "CLAUDE.md"))
                                    or os.path.isfile(os.path.join(dp, "AGENTS.md")),
                     "has_skills": os.path.isdir(os.path.join(dp, ".claude", "skills"))})
    return sorted(pkgs, key=lambda p: -p["files"])


def check_splits(root, pkgs):
    """When to split: a package big enough to have its own conventions, with
    no local context, is paying ancestor-file tax and getting nothing back."""
    for p in pkgs:
        if p["has_context"] or p["files"] < SPLIT_MIN_FILES:
            continue
        add("low", "split", p["dir"],
            f"{p['files']} {p['stack']} files, no local instruction file",
            f"Add {p['dir']}/CLAUDE.md for conventions specific to this package "
            "(loads lazily — only when the agent touches it, so it costs nothing elsewhere).")
    stacks = {p["stack"] for p in pkgs}
    if len(stacks) > 1 and not any(p["has_context"] for p in pkgs):
        add("medium", "split", ".",
            f"{len(stacks)} stacks in one tree ({', '.join(sorted(stacks))}) with no per-package context",
            "Per-package instruction files keep each stack's conventions out of the others' sessions.")


# ---------------------------------------------------------------- plan mode

def emit_plan(root, pkgs, seen):
    has_root = any(os.path.dirname(os.path.abspath(p)) == os.path.abspath(root)
                   for p, anc in seen if not anc)
    ancestors = [rel(root, p) for p, anc in seen if anc]
    print(f"Proposed agent context layout for {os.path.abspath(root)}\n")
    if ancestors:
        print("Inherited (loaded eagerly into every session here):")
        for a in ancestors:
            print(f"  {a}")
        print("  ^ keep these small and universally true — they tax every session below.\n")
    print(f"{os.path.basename(os.path.abspath(root)) or '.'}/")
    print(f"├── CLAUDE.md{'' if has_root else '        <- CREATE'}"
          f"          # always-true rules only, <{CLAUDE_MD_LINES} lines")
    print("├── .claude/")
    print("│   ├── skills/          # sometimes-needed reference + workflows (loaded on demand)")
    print("│   └── rules/           # symlink shared house rules in here, don't copy them")
    print("├── .mcp.json            # project scope, committed; ${VAR} for every secret")
    if pkgs:
        for p in pkgs[:12]:
            flag = "" if p["has_context"] else "   <- CREATE CLAUDE.md"
            print(f"├── {p['dir']}/  ({p['stack']}, {p['files']} files){flag}")
    print("\nRules this layout encodes:")
    print(f"  - Root file stays under {CLAUDE_MD_LINES} lines; detail goes to skills.")
    print("  - Per-package files load ONLY when the agent touches that package.")
    print("  - Shared content is referenced (symlink/plugin), never copied.")
    print("  - Launch the agent in the package you're working in, not at the root.")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Design and audit an agent context layout.")
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--plan", action="store_true", help="propose a layout instead of auditing")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--fail-on", default="high", choices=["high", "medium", "low", "never"])
    ap.add_argument("--portable", action="store_true",
                    help="repo is public or has outside contributors: everything it needs "
                         "must live IN the repo")
    a = ap.parse_args()

    root = os.path.abspath(a.path)
    if not os.path.isdir(root):
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    seen = check_instruction_files(root)
    pkgs = scan_packages(root)
    if a.plan:
        emit_plan(root, pkgs, seen)
        return 0

    check_root_context(root, seen, a.portable)
    check_local_paths(root)
    check_portability(root, a.portable)
    check_adapters(root, a.portable)
    check_agents_md_pairing(root, seen)
    check_skills_and_memory(root)
    check_mcp_config(root)
    check_drift(root, seen)
    check_splits(root, pkgs)
    findings.sort(key=lambda f: (SEV_ORDER[f[0]], f[1], f[2]))

    if a.json:
        print(json.dumps({
            "root": root,
            "packages": pkgs,
            "findings": [{"severity": s, "kind": k, "path": p,
                          "message": m, "action": act} for s, k, p, m, act in findings],
        }, indent=2))
    else:
        print(f"Workspace doctor — {root}\n")
        if not findings:
            print("  No findings. Layout matches the standard.")
        for sev, kind, path, msg, action in findings:
            print(f"  [{sev.upper():6}] {kind:10} {path}")
            print(f"           {msg}")
            print(f"           -> {action}\n")
        counts = {}
        for s, *_ in findings:
            counts[s] = counts.get(s, 0) + 1
        if counts:
            print("  " + ", ".join(f"{v} {k}" for k, v in
                                   sorted(counts.items(), key=lambda x: SEV_ORDER[x[0]])))
        if pkgs:
            print(f"  {len(pkgs)} package(s); "
                  f"{sum(1 for p in pkgs if p['has_context'])} with local context")

    if a.fail_on == "never":
        return 0
    threshold = SEV_ORDER[a.fail_on]
    return 1 if any(SEV_ORDER[s] <= threshold for s, *_ in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
