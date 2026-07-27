#!/usr/bin/env python3
"""PII scanner — commit-time gate, CI gate, and history auditor.

Three modes, one detector:

    pii_scan.py --staged     # pre-commit: what is about to enter history
    pii_scan.py --tree       # CI: the working tree / checkout
    pii_scan.py --history    # audit: every blob ever committed

Why commit-time is the load-bearing one: **PII in git history is effectively
permanent.** Rewriting a pushed repo is disruptive, and once it is public,
cloned, or forked it cannot be guaranteed. The only cheap moment is before the
commit exists.

⚠️ THIS TOOL NEVER PRINTS A MATCHED VALUE. It reports type, file, and line only.
A scanner that echoes PII into a CI log has just published it somewhere more
widely readable than the repo. Findings carry no `value` field at all, so it
cannot regress into leaking one.

Detection is a NET, not a wall. Structured PII (SSN, cards, email, phone) is
caught reliably; names and street addresses are not. The primary control is
architectural — PII should be absent by construction (see
references/pii-controls.md). Treat a clean scan as "no known pattern found",
never as "no PII present".
"""
import os, re, sys, json, argparse, subprocess

# ---------------------------------------------------------------- detectors
# (name, regex, confidence, tier) — tier is the minimum data class it implies.
# confidence: high = structurally distinctive; low = shape-matched, expect noise.
#
# SECRETS are delegated to gitleaks when it is installed (see run_gitleaks):
# it has a far larger, actively-maintained ruleset than anything worth
# hand-writing. These patterns are the FALLBACK for machines without it — which
# is not hypothetical; the machine this was written on did not have it. A
# scanner that hard-fails on a missing binary gets removed from the hook.
SECRET_DETECTORS = [
    ("private_key",   r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----","high", "P3"),
    ("aws_key",       r"\bAKIA[0-9A-Z]{16}\b",                                 "high", "P3"),
]

# PII stays ours. Presidio (the obvious adopt) has NO street-address entity —
# its LOCATION is generic geo NER — and the real incident here was an address
# in a docstring. Nothing off the shelf covers this set at commit-time speed.
PII_DETECTORS = [
    ("ssn",           r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b", "high", "P3"),
    ("credit_card",   r"\b(?:\d[ -]?){13,19}\b",                               "high", "P3"),
    ("email",         r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",   "high", "P1"),
    ("phone_us",      r"\b(?:\+1[ -.]?)?\(?\d{3}\)?[ -.]\d{3}[ -.]\d{4}\b",    "high", "P2"),
    ("iban",          r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",                     "low",  "P3"),
    ("date_of_birth", r"(?i)\b(?:dob|date[ _]of[ _]birth)\b\s*[:=]\s*\S+",     "high", "P3"),
    ("street_address", r"(?i)\b\d{1,5}\s+[A-Z][A-Za-z]{2,}(?:\s+[A-Z][A-Za-z]{2,}){0,3}\s+"
                       r"(?:st|street|rd|road|ave|avenue|dr|drive|ln|lane|blvd|boulevard|ct|court|way|pl|place)\b",
                       "low", "P2"),
    ("us_zip_state",  r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b",                      "low",  "P2"),
]
DETECTORS = PII_DETECTORS + SECRET_DETECTORS
TIER_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# Obvious placeholders. Real data does not look like this, and flagging it
# trains people to ignore the tool.
BENIGN = re.compile(
    r"(?i)("
    r"example\.(com|org|net)|@example|localhost|test@|foo@|bar@|user@|"
    # NANP reserves the 555 area/exchange for fiction; 000/123/999 are the other
    # conventional placeholders. Without these, every template and test fixture
    # in the repo lights up — and a scanner that cries wolf gets switched off.
    r"\(?555\)?[ -.]?\d{3}[ -.]?\d{4}|\b555-?\d{4}\b|"
    r"\(?(?:000|123|999)\)?[ -.]?\d{3}[ -.]?\d{4}|"
    r"123[ -]?456[ -]?7890|"
    r"000-00-0000|123-45-6789|4111[ -]?1111[ -]?1111[ -]?1111|"
    r"xxx|yyy|zzz|placeholder|redacted|\bfake\b|\bdummy\b|\bsample\b|"
    r"«[A-Z_]+\d*»|\bJane Doe\b|\bJohn Doe\b|\$\{|\{\{|"
    r"<[a-z_]+>|your[-_ ]?(name|email|phone|address)"
    r")")

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
             "build", ".next", "target", "vendor", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", "coverage", ".tox", "site-packages"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".tar",
            ".woff", ".woff2", ".ttf", ".ico", ".mp4", ".webp", ".svg",
            ".lock", ".min.js", ".map", ".bin", ".so", ".dll", ".pyc"}
ALLOW_FILE = ".pii-allow"


def luhn(s):
    """Card detector without Luhn is mostly a long-number detector."""
    d = [int(c) for c in re.sub(r"\D", "", s)]
    if not 13 <= len(d) <= 19:
        return False
    tot, alt = 0, False
    for n in reversed(d):
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        tot += n
        alt = not alt
    return tot % 10 == 0


def load_allow(root):
    """Per-repo allowlist: one regex per line, # comments. For values that are
    genuinely safe (docs examples, synthetic fixtures) — reviewed, not assumed."""
    pats = []
    p = os.path.join(root, ALLOW_FILE)
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    pats.append(re.compile(line))
                except re.error:
                    print(f"warning: bad regex in {ALLOW_FILE}: {line}", file=sys.stderr)
    return pats


IGNORE_FILE_MARKER = "pii-scan: ignore-file"


def scan_text(text, path, allow, min_conf, detectors=None):
    """Return findings. NO matched value is ever captured — by construction."""
    out = []
    if os.path.basename(path) == os.path.basename(__file__):
        return out                       # the detector's own patterns are not findings
    # A file may declare itself a deliberate synthetic-PII corpus with the marker
    # in its first few lines (mirrors gitleaks' `:allow`). Visible and reviewable
    # in the diff — a reviewer sees the marker and asks why. Use ONLY for files
    # whose entire purpose is to hold fixture data (the scanner's own test corpus).
    if IGNORE_FILE_MARKER in "\n".join(text.splitlines()[:8]):
        return out
    for lineno, line in enumerate(text.splitlines(), 1):
        if len(line) > 4000:
            continue                     # minified/gen'd; skip rather than churn
        for name, pat, conf, tier in (detectors or DETECTORS):
            if conf == "low" and min_conf == "high":
                continue
            for m in re.finditer(pat, line):
                frag = m.group(0)
                if BENIGN.search(frag) or BENIGN.search(line):
                    continue
                if any(a.search(line) for a in allow):
                    continue
                if name == "credit_card":
                    if not luhn(frag):
                        continue
                    # A decimal fraction (the tail of a lat/long coordinate,
                    # -72.123456789012) passes Luhn by chance. A card is never a
                    # decimal fraction, so a preceding '.' rules it out. This was
                    # ~all of the hits on geodata files.
                    if m.start() > 0 and line[m.start() - 1] == ".":
                        continue
                    # A 16-digit run passes Luhn ~1 time in 10 by chance, so two ID
                    # shapes dominated this detector's real-world hits — both need
                    # suppressing without also suppressing a real card:
                    #  (a) the numeric tail of a UUID (abababab-1111-2222-...).
                    #      Detected by matching the canonical UUID shape ANYWHERE
                    #      in the line and skipping if our match overlaps it. A
                    #      hyphen-grouped card (4532-0151-1283-0366) has no hex
                    #      letters, so it does not match the UUID shape and is kept.
                    #  (b) a value under an identifier-named key (webhookId, sha).
                    if any(u.start() <= m.start() < u.end() for u in
                           re.finditer(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                                       r"[0-9a-f]{4}-[0-9a-f]{12}", line, re.I)):
                        continue
                    if re.search(r'"\w*(?:id|uuid|key|hash|token|sha)\w*"\s*:\s*"?[\w-]*$',
                                 line[:m.start()], re.I):
                        continue
                if name == "us_zip_state" and not re.search(r"\d", line):
                    continue
                out.append({"type": name, "confidence": conf, "implies_tier": tier,
                            "file": path, "line": lineno})
                break                    # one finding per detector per line
    return out


TERMS_FILE = ".pii-terms"
# A quoted run of three or more words. DOUBLE quotes only: treating the apostrophe
# as a delimiter makes an ordinary possessive ("the team's own") close a quote
# opened earlier in the sentence, which blocks valid messages. A gate with false
# positives gets switched off.
_QUOTED = re.compile(r"[\"“”]([^\"“”\n]{12,}?\s+\S+\s+\S+[^\"“”\n]*)[\"“”]")


def term_files(root):
    """Every `.pii-terms` that applies to `root`, most global first.

    1. `$PII_TERMS_HOME/.pii-terms`, else `~/.pii-terms` — MACHINE-GLOBAL.
    2. `<parent of root>/.pii-terms` — the workspace holding sibling repos.
    3. `<root>/.pii-terms` — repo-local (the original, and still supported).

    Why the global file is the important one: a term listed only in the repo it
    was noticed in protects that repo alone, so the same employer or client name
    stays exposed in the other seven. And the global file lives OUTSIDE every
    repo, so it cannot be committed to a public one — which matters, because a
    tracked list of the words you are hiding is itself the disclosure.
    """
    home = os.environ.get("PII_TERMS_HOME") or os.path.expanduser("~")
    parent = os.path.dirname(os.path.abspath(root))
    seen, out = set(), []
    for base in (home, parent, os.path.abspath(root)):
        p = os.path.normcase(os.path.join(base, TERMS_FILE))
        if p not in seen:          # root's parent can BE home; don't read twice
            seen.add(p)
            out.append(os.path.join(base, TERMS_FILE))
    return out


def load_terms(root):
    """Private nouns that must never appear in a commit message.

    One per line in a GITIGNORED `.pii-terms` — employer, client, patient, or
    codename vocabulary. UNION of every file in `term_files()`, so the vocabulary
    is defined once per machine rather than re-listed per repo.
    """
    out = []
    for p in term_files(root):
        if not os.path.isfile(p):
            continue
        for line in read(p).splitlines():
            t = line.strip()
            if t and not t.startswith("#") and len(t) > 3:
                out.append(t.lower())
    return sorted(set(out))


# Ignored, but machine-generated: caches and build output never hold the
# human-written prose this check is looking for. Skipping them is not just a
# speed win — a cache directory can hold thousands of files and would exhaust
# the scan budget before reaching the private notes that matter.
NOISE_DIRS = {
    "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache", ".tox",
    "node_modules", ".venv", "venv", "env", "dist", "build", ".next", ".cache",
    ".gradle", "target", ".terraform", ".idea", ".vscode",
}


def ignored_files(root, limit=400):
    """Paths git is ignoring — i.e. the files deliberately kept out of history."""
    proc = git(root, "status", "--porcelain", "--ignored=matching")
    paths = []
    for line in (proc.stdout or "").splitlines():
        if not line.startswith("!!"):
            continue
        rel = line[3:].strip().strip('"')
        if any(part in NOISE_DIRS for part in rel.replace("\\", "/").split("/")):
            continue
        full = os.path.join(root, rel)
        if os.path.isdir(full):
            for dirpath, dirs, files in os.walk(full):
                dirs[:] = [d for d in dirs if d not in NOISE_DIRS]  # prune in place
                for fn in files:
                    paths.append(os.path.join(dirpath, fn))
                    if len(paths) >= limit:
                        return paths
        else:
            paths.append(full)
        if len(paths) >= limit:
            break
    return paths


def scan_message(root, text, allow, min_conf, detectors=None):
    """Findings for a commit message.

    WHY MESSAGES NEED THEIR OWN MODE: gitignore, tokenization, and synthetic
    fixtures all protect FILES. A commit message is prose written *about* those
    files, it is permanent and public, and no other control in this lens looks at
    it. The observed leak was narrative — a private document quoted to explain the
    fix — which every structured detector misses by design.

    Three layers, cheapest first:
      1. the normal detectors, so a pasted address or phone number is caught;
      2. `.pii-terms`, the repo's private vocabulary, which is what catches proper
         nouns — the class the lens states outright is not reliably detectable;
      3. quoted runs that appear VERBATIM in a gitignored file. This one needs no
         configuration and generalizes: if you quoted something that lives in a
         file you deliberately kept out of the repo, you just put it in the repo.
    """
    findings = list(scan_text(text, "<commit message>", allow, min_conf, detectors))

    low = text.lower()
    for term in load_terms(root):
        if re.search(rf"\b{re.escape(term)}\b", low):
            findings.append({"type": "private_term", "confidence": "high",
                             "implies_tier": "P2", "file": "<commit message>", "line": 1})

    quotes = [m.group(1).strip() for m in _QUOTED.finditer(text)]
    if quotes:
        # Only pay for this when the message actually quotes something.
        for path in ignored_files(root):
            try:
                if os.path.getsize(path) > 2_000_000:
                    continue
            except OSError:
                continue
            body = read(path)
            if not body:
                continue
            for q in quotes:
                if q.lower() in body.lower():
                    findings.append({"type": "quoted_from_ignored_file", "confidence": "high",
                                     "implies_tier": "P2", "file": "<commit message>", "line": 1})
                    quotes = [x for x in quotes if x != q]
            if not quotes:
                break
    return findings


def iter_tree(root):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in fn:
            if os.path.splitext(f)[1].lower() in SKIP_EXT:
                continue
            yield os.path.join(dp, f)


def read(p):
    try:
        with open(p, encoding="utf-8", errors="strict") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None                      # binary or unreadable: nothing to scan


def git(root, *args):
    # encoding= is explicit and load-bearing. text=True alone decodes with the
    # LOCALE default, which is cp1252 on a stock Windows box — so every
    # non-ASCII byte in a staged blob came back as mojibake ("«" -> "Â«"). That
    # is not cosmetic in a security gate: a .pii-allow regex or a detector
    # pattern containing any non-ASCII character silently stops matching, and
    # the scan reports clean because it never saw the text it was looking for.
    # Git emits UTF-8; decode it as UTF-8 everywhere. errors="replace" still
    # covers a genuinely binary blob.
    return subprocess.run(["git", "-C", root, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


# ------------------------------------------------- gitleaks (secrets backend)

def have_gitleaks():
    try:
        r = subprocess.run(["gitleaks", "version"], capture_output=True, text=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def file_has_ignore_marker(path):
    """Does this file declare itself a deliberate synthetic-fixture corpus?

    Shared by the built-in scanner and the gitleaks bridge so one marker governs
    both engines. Reads only the head of the file: the marker is a declaration,
    not something to be found buried on line 900.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = "".join(next(fh, "") for _ in range(8))
    except OSError:
        return False
    return IGNORE_FILE_MARKER in head


def run_gitleaks(root, mode):
    """Delegate secret detection to gitleaks, and impose our no-leak rule on it.

    gitleaks does NOT redact by default — its `--redact` flag registers with a
    default of 0, i.e. off — so raw secrets go to stdout unless asked otherwise.
    Two defences, because one is not enough for something this easy to get wrong:

      1. `--redact` is pinned into the argv here, never optional.
      2. We read ONLY RuleID / File / StartLine out of its JSON. Any field that
         could carry a value (`Secret`, `Match`, `Line`) is never touched, so
         even if redaction silently regressed upstream, nothing leaks through us.
    """
    # gitleaks v8.19+ REPLACED `detect`/`protect` with `dir`/`git`, and by v8.30
    # the old names are gone entirely. The previous mapping here targeted a CLI
    # that no longer exists: the call failed, the parse failed, and the scanner
    # quietly fell back to built-in patterns — reporting "engine: built-in only"
    # forever on machines that HAD gitleaks installed. The source is a positional
    # argument now, not --source.
    sub = {"staged": ["git", "--staged"], "tree": ["dir"], "history": ["git"]}[mode]
    cmd = ["gitleaks", *sub, root, "--redact",
           "--report-format", "json", "--report-path", "-", "--no-banner", "--exit-code", "0"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        return None                      # not installed — the documented, quiet path
    except (OSError, subprocess.SubprocessError) as e:
        print(f"warning: gitleaks failed ({e}); falling back to built-in patterns",
              file=sys.stderr)
        return None
    try:
        raw = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        # gitleaks IS installed and we still could not read it — almost always a
        # version mismatch. Say so loudly. "Installed but silently unused" is the
        # failure this whole lens keeps finding, and it must not be indistinguishable
        # from "not installed".
        print("warning: gitleaks is INSTALLED but its output was unparseable — "
              "expected the v8.19+ CLI (`dir`/`git`). Falling back to built-in "
              "patterns, which detect far less.", file=sys.stderr)
        head = (r.stderr or "").strip().splitlines()[:2]
        for line in head:
            print(f"  gitleaks said: {line}", file=sys.stderr)
        return None
    # gitleaks `dir` walks the FILESYSTEM and does not honour .gitignore. Our
    # architecture deliberately puts real secrets in gitignored paths — .env is
    # in the ignore block this very tool installs — so unfiltered it reports the
    # externalize control WORKING as if it were a leak. Restrict it to what git
    # would actually include, exactly as the built-in scanner does.
    # tracked_or_untracked() returns ABSOLUTE paths; normalise to repo-relative
    # so the membership test can actually match. Comparing the two shapes
    # directly would silently drop EVERY gitleaks finding — a filter that
    # discards everything looks identical to a clean scan.
    root_abs = os.path.abspath(root).replace("\\", "/").rstrip("/")
    _inc = tracked_or_untracked(root)
    included = None if _inc is None else {
        os.path.relpath(p, root).replace("\\", "/") for p in _inc}

    out = []
    for f in raw or []:
        rel = (f.get("File") or "").replace("\\", "/")
        # `dir` mode reports absolute paths; `git` mode reports repo-relative.
        if rel.startswith(root_abs + "/"):
            rel = rel[len(root_abs) + 1:]
        rel = rel.lstrip("./")
        if included is not None and rel not in included:
            continue
        # ONE exemption mechanism, not two. gitleaks findings are appended after
        # scan_text, so without this the `pii-scan: ignore-file` marker would
        # govern the built-in engine and be silently ignored by gitleaks — a file
        # deliberately full of fixture credentials would pass on a machine
        # without gitleaks and fail on one with it. Same marker, both engines.
        #
        # File-level only, deliberately: we never read gitleaks' Line/Match/Secret
        # fields, so there is nothing to apply a line-level `.pii-allow` regex to.
        if rel and file_has_ignore_marker(os.path.join(root, rel)):
            continue
        out.append({
            "type": f"secret:{f.get('RuleID') or 'unknown'}",
            "confidence": "high", "implies_tier": "P3",
            "file": rel,
            "line": f.get("StartLine") or 0,
            "engine": "gitleaks",
        })
    return out


def scan_staged(root, allow, min_conf, detectors=None):
    r = git(root, "diff", "--cached", "--name-only", "--diff-filter=ACM")
    findings = []
    for rel in [x for x in r.stdout.splitlines() if x.strip()]:
        if os.path.splitext(rel)[1].lower() in SKIP_EXT:
            continue
        blob = git(root, "show", f":{rel}")
        if blob.returncode == 0 and blob.stdout:
            findings += scan_text(blob.stdout, rel, allow, min_conf, detectors)
    return findings


def tracked_or_untracked(root):
    """Files git would actually include — tracked plus untracked-not-ignored.
    Respecting .gitignore is not a nicety here: the externalize control PUTS
    real data in gitignored paths (personal/, data/private/) on purpose.
    Scanning those fights our own model and floods noise. Returns None outside
    a git repo, where we fall back to walking the whole tree."""
    r = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if r.returncode != 0:
        return None
    return [os.path.join(root, p) for p in r.stdout.split("\0") if p]


def scan_tree(root, allow, min_conf, detectors=None):
    findings = []
    listed = tracked_or_untracked(root)
    paths = listed if listed is not None else list(iter_tree(root))
    for p in paths:
        if os.path.splitext(p)[1].lower() in SKIP_EXT:
            continue
        parts = set(os.path.normpath(p).split(os.sep))
        if parts & SKIP_DIRS:
            continue
        t = read(p)
        if t is not None:
            findings += scan_text(t, os.path.relpath(p, root).replace("\\", "/"),
                                  allow, min_conf, detectors)
    return findings


def scan_history(root, allow, min_conf, detectors=None, limit=None):
    """Every blob ever committed. This is the expensive one, and the only one
    that answers 'is it already too late'."""
    r = git(root, "rev-list", "--all")
    commits = [c for c in r.stdout.splitlines() if c.strip()]
    if limit:
        commits = commits[:limit]
    seen, findings = set(), []
    for c in commits:
        tr = git(root, "ls-tree", "-r", c)
        for line in tr.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            sha, rel = parts[2], line.split("\t", 1)[-1]
            if sha in seen or os.path.splitext(rel)[1].lower() in SKIP_EXT:
                continue
            seen.add(sha)
            blob = git(root, "cat-file", "-p", sha)
            if blob.returncode == 0 and blob.stdout:
                for f in scan_text(blob.stdout, rel, allow, min_conf, detectors):
                    f["commit"] = c[:12]
                    findings.append(f)
    return findings, len(commits), len(seen)


def main():
    ap = argparse.ArgumentParser(description="Scan for PII. Never prints matched values.")
    ap.add_argument("root", nargs="?", default=".")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true", help="pre-commit: staged content")
    g.add_argument("--tree", action="store_true", help="CI: working tree (default)")
    g.add_argument("--history", action="store_true", help="audit: all committed blobs")
    g.add_argument("--message", metavar="FILE",
                   help="commit-msg: the message about to enter history")
    ap.add_argument("--visibility", choices=["public", "private"], default="private",
                    help="public repos scan at higher sensitivity")
    ap.add_argument("--max-tier", choices=["P0", "P1", "P2", "P3"], default="P1",
                    help="highest data class allowed in this repo (default P1)")
    ap.add_argument("--history-limit", type=int, default=None)
    ap.add_argument("--no-gitleaks", action="store_true",
                    help="skip the gitleaks backend even if installed")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    root = os.path.abspath(a.root)
    allow = load_allow(root)
    # Public repos get the noisy low-confidence detectors too: a false positive
    # costs a minute, a missed address in a public repo cannot be undone.
    min_conf = "low" if a.visibility == "public" else "high"

    # Secrets: gitleaks when present (bigger, maintained ruleset), our two
    # fallback patterns otherwise. Which engine ran is REPORTED, so a clean
    # result is never over-trusted.
    use_gl = have_gitleaks() and not a.no_gitleaks
    detectors = PII_DETECTORS if use_gl else DETECTORS
    engine = "gitleaks + built-in PII" if use_gl else "built-in only (gitleaks not installed)"

    scanned = ""
    if a.message:
        raw = read(os.path.abspath(a.message))
        # git strips comment lines before storing; scanning them would flag the
        # diff summary `--verbose` injects and block on the user's own files.
        body = "\n".join(ln for ln in raw.splitlines() if not ln.startswith("#"))
        findings, mode = scan_message(root, body, allow, min_conf, detectors), "message"
    elif a.staged:
        findings, mode = scan_staged(root, allow, min_conf, detectors), "staged"
    elif a.history:
        findings, ncommits, nblobs = scan_history(root, allow, min_conf,
                                                  detectors=detectors, limit=a.history_limit)
        mode = "history"
        scanned = f"{ncommits} commits, {nblobs} unique blobs"
    else:
        findings, mode = scan_tree(root, allow, min_conf, detectors), "tree"

    # gitleaks scans files and git objects; it has no notion of a pending message,
    # so message mode uses the built-in detectors only and says so.
    if use_gl and mode == "message":
        use_gl, engine = False, "built-in only (gitleaks does not scan messages)"
    if use_gl:
        gl = run_gitleaks(root, mode)
        if gl is None:                      # gitleaks broke — degrade, don't fail
            engine = "built-in only (gitleaks errored)"
            findings += scan_tree(root, allow, min_conf, SECRET_DETECTORS) if not a.staged                         else scan_staged(root, allow, min_conf, SECRET_DETECTORS)
        else:
            findings += gl

    ceiling = TIER_ORDER[a.max_tier]
    blocking = [f for f in findings if TIER_ORDER[f["implies_tier"]] > ceiling]

    if a.json:
        print(json.dumps({"mode": mode, "engine": engine, "visibility": a.visibility,
                          "max_tier": a.max_tier, "scanned": scanned,
                          "findings": findings, "blocking": len(blocking)}, indent=2))
    else:
        print(f"PII scan — {mode}" + (f" ({scanned})" if scanned else "")
              + f" · visibility={a.visibility} · max-tier={a.max_tier}")
        print(f"  engine: {engine}")
        if not findings:
            print("  No known PII patterns found.")
            print("  NOTE: detection is a net, not a wall — names and many addresses")
            print("        are not reliably detectable. Absence of findings is not proof.")
        for f in findings:
            mark = "BLOCK" if f in blocking else "note "
            loc = f"{f['file']}:{f['line']}"
            commit = f"  [{f['commit']}]" if "commit" in f else ""
            print(f"  {mark} {f['type']:15} ({f['confidence']:4}, implies {f['implies_tier']})  {loc}{commit}")
        if blocking:
            print(f"\n  {len(blocking)} finding(s) exceed this repo's max data class ({a.max_tier}).")
            print(f"  Fix by removing the value, externalizing it, or tokenizing it —")
            print(f"  see references/pii-controls.md. If genuinely safe, add a regex to {ALLOW_FILE}.")
            if mode == "history":
                print("  HISTORY findings cannot be fixed by editing the file: the blob")
                print("  is already committed. See the remediation section of the lens.")

    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
