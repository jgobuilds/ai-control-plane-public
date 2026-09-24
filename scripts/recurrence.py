#!/usr/bin/env python3
"""The recurrence register — seen once, fix it; seen twice, fix the harness.

WHY THIS EXISTS. This repo already had three good habits for a failure: fix it,
write a known-cause row (`scripts/ci-known-causes.json`), write a solution
(`docs/solutions/`). What it did not have was a COUNT. Nothing anywhere added up
how many times the same thing had happened, so "this keeps failing" stayed a
human impression — and the impression was right: five identical Dependabot
failures ran daily for a week with nobody escalating, because each individual
morning it was just one red run.

The register makes the count a fact and attaches a rule to it:

    1st occurrence   fix the instance. No paperwork. Not every failure is a
                     pattern, and demanding a register entry for every red run
                     is how registers get abandoned.
    2nd occurrence   fix the HARNESS. Twice is a pattern, and a pattern that
                     only a human notices will be noticed later and later. The
                     gate now demands a DISPOSITION, and a disposition of
                     "fixed" must name the command that proves it.

`asserted_by` is lifted straight from `security/findings.yaml`, for the same
reason it exists there: a claim nobody runs is not a verified claim. Command
plus an expected substring — a suite that still exits zero after someone deletes
the one check that mattered satisfies "exit 0" and fails this.

    python scripts/recurrence.py collect              # append what happened
    python scripts/recurrence.py check --shape        # CI: register is honest
    python scripts/recurrence.py check                # host: counts + honest
    python scripts/recurrence.py report               # human / --json for a lane
    python scripts/recurrence.py sources              # can each source be read now?

TWO CHECK MODES, ON PURPOSE. Occurrences live in `audit/`, which is gitignored
runtime evidence — so in CI there is no occurrence log at all, and a counting
gate there would pass while counting nothing. `--shape` checks what CI can
actually see: the register's own honesty (proofs run, nothing expired). The
counting half runs where the data is. A gate that cannot see its data reports
`unknown`, never `pass`.

Sources are declared, and one that cannot be read says so. A collector that
silently contributes zero looks exactly like a quiet week.

Stdlib + pyyaml (already a CI dependency for security/findings.yaml).
"""
from __future__ import annotations
import argparse, datetime as _dt, hashlib, json, os, re, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.environ.get("AICP_RECURRENCE_LOG", os.path.join(ROOT, "audit", "recurrence.jsonl"))
REGISTER = os.environ.get("AICP_RECURRENCE_REGISTER",
                          os.path.join(ROOT, "reliability", "recurrence.yaml"))
AUDIT = os.environ.get("AICP_AUDIT_LOG", os.path.join(ROOT, "audit", "decisions.jsonl"))

THRESHOLD = 2          # the doctrine, in one constant
WINDOW_DAYS = 90
# A source silent longer than this is reported STALE. Sized to the cadence it
# watches: CI runs most days here, so two weeks of nothing means the collector
# stopped, not that the week was quiet.
STALE_DAYS = 14
# n8n's public API caps `limit` at 250. Reported on every collect, never silent.
LANE_LIMIT = 100
STATUSES = ("harness-fixed", "accepted", "watching")

# Noise that is different on every run and identical in meaning. Order matters:
# hex before digits, or every hex run becomes <n><n><n>.
NOISE = [
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[t ][\d:.]+z?\b", re.I), "<ts>"),
    (re.compile(r"\bjob_\d+\b", re.I), "<job>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def rel(path):
    """os.path.relpath raises across Windows drives — and a fixture in %TEMP% on
    C: with the repo on D: is exactly that case. A path shown in a message is
    never worth an exception."""
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return str(path)


def now():
    return _dt.datetime.now(_dt.timezone.utc)


def today():
    return now().date()


def normalise(line):
    """A log line reduced to what is the SAME across two occurrences.

    Everything a rerun changes — timestamps, container ids, job numbers, counts —
    is what makes two identical failures look like two different ones, so it all
    goes. What is left is short enough to read in the register and specific
    enough that two unrelated failures do not collapse into one entry.
    """
    s = str(line or "").strip()
    # gh's log format is "<workflow>\t<step>\t<timestamp> <message>".
    if "\t" in s:
        s = s.rsplit("\t", 1)[-1]
    s = re.sub(r"^\ufeff?\d{4}-\d{2}-\d{2}T[\d:.]+Z\s*", "", s)
    s = re.sub(r"^##\[(error|warning)\]", "", s, flags=re.I)
    s = s.lower()
    for pat, rep in NOISE:
        s = pat.sub(rep, s)
    return s.strip()[:120]


# ---------------------------------------------------------------- occurrences

def read_log(path=None, window_days=WINDOW_DAYS):
    """Occurrences inside the window. Missing file is a state, not an error."""
    path = path or LOG
    if not os.path.isfile(path):
        return None                      # None means "no data", not "no failures"
    cutoff = (now() - _dt.timedelta(days=window_days)).isoformat()
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if str(r.get("ts", "")) >= cutoff:
                out.append(r)
    return out


def append(records, path=None):
    """Append, skipping any occurrence id already present. Collect is idempotent —
    running it twice must not turn one failure into a pattern."""
    path = path or LOG
    seen = set()
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    seen.add(json.loads(line).get("oid"))
                except ValueError:
                    continue
    fresh = [r for r in records if r.get("oid") not in seen]
    if fresh:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            for r in fresh:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
    return fresh


def counted(occurrences):
    """{signature: {count, first, last, sources, detail}} — biggest first."""
    agg = {}
    for r in occurrences:
        sig = r.get("signature") or "unknown"
        a = agg.setdefault(sig, {"count": 0, "first": None, "last": None,
                                 "sources": set(), "detail": r.get("detail", "")})
        a["count"] += 1
        ts = r.get("ts")
        a["first"] = min(a["first"], ts) if a["first"] else ts
        a["last"] = max(a["last"], ts) if a["last"] else ts
        a["sources"].add(r.get("source", "?"))
    for a in agg.values():
        a["sources"] = sorted(a["sources"])
    return dict(sorted(agg.items(), key=lambda kv: -kv[1]["count"]))


# -------------------------------------------------------------------- sources

def gh_bin():
    # gh is routinely installed off-PATH on Windows, which is where this repo
    # lives; a bare which() lookup finds nothing and the source goes quiet.
    for cand in (os.environ.get("GH_BIN"), shutil.which("gh"),
                 r"C:\Program Files\GitHub CLI\gh.exe"):
        if not cand:
            continue
        if os.path.isabs(cand) and not os.path.isfile(cand):
            continue
        return cand
    return None


def _run(argv, timeout=120):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=ROOT)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", f"{type(e).__name__}: {e}"


def source_ci(repo=None, limit=60):
    """Failed GitHub Actions runs, signed by their first real error line.

    The known-cause table is matched FIRST, exactly as ci_diagnose does: a hit
    gives a stable id instead of a line that shifts when someone rewords an error
    message. That is also where a diagnosed failure is supposed to be promoted
    to, so the two tables reinforce each other.
    """
    gh = gh_bin()
    if not gh:
        return [], "gh not found (set GH_BIN); CI history unavailable"
    repo = repo or os.environ.get("AICP_GH_REPO", "")
    argv = [gh, "run", "list", "--limit", str(limit), "--json",
            "conclusion,workflowName,createdAt,databaseId,displayTitle"]
    if repo:
        argv += ["-R", repo]
    code, out, err = _run(argv)
    if code != 0:
        return [], f"gh run list failed: {err.strip()[:120]}"
    try:
        runs = json.loads(out)
    except ValueError:
        return [], "gh run list returned unparseable json"

    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import ci_diagnose                                  # noqa: WPS433
        causes = ci_diagnose.load_causes()
        redact = ci_diagnose.redact
    except Exception:                                       # noqa: BLE001
        causes, redact = [], lambda t: t

    recs = []
    for run in runs:
        if run.get("conclusion") not in ("failure", "timed_out", "startup_failure"):
            continue
        rid = run.get("databaseId")
        argv = [gh, "run", "view", str(rid), "--log-failed"]
        if repo:
            argv += ["-R", repo]
        code, log, _ = _run(argv, timeout=180)
        sig, detail = None, ""
        if code == 0 and log.strip():
            log = redact(log)
            try:
                hit = ci_diagnose.match_known(log, causes)
            except Exception:                               # noqa: BLE001
                hit = None
            if hit:
                got = hit[0] if isinstance(hit, (list, tuple)) else hit
                cid = got.get("id") if isinstance(got, dict) else None
                if cid:
                    sig, detail = f"ci:cause:{cid}", str(got.get("cause", ""))[:200]
            if not sig:
                sig, detail = _ci_signature(log, run.get('workflowName', ''))
        if not sig:
            sig = f"ci:{run.get('workflowName', '?')}:{run.get('conclusion')}"
            detail = "no log available for this run"
        recs.append({"ts": run.get("createdAt") or now().isoformat(),
                     "source": "ci", "oid": f"ci:{rid}", "signature": sig,
                     "detail": detail[:200],
                     "context": run.get("workflowName", "")})
    return recs, None


# Lines that every failed run emits. `##[error]Process completed with exit code 1`
# is the runner announcing that a step failed, not what failed — signing on it
# would bucket a broken linter, a failing test and a missing secret together as
# one 40x recurrence, which is worse than not counting at all. Used as a
# LAST RESORT, not a filter: a run whose log offers nothing else still gets a
# signature, because an imprecise count beats a silent one.
# Box-drawing characters mean we are looking at a RENDERED TABLE, not a message.
# Trivy prints its findings as a table, and `failing_lines` happily ranked
# "| es/redis-errors/package.json | | |" as the strongest failing line for 13
# consecutive security-scan failures. The count was right and the label was
# unreadable, which makes a recurrence impossible to act on.
TABLE_CHARS = set("│┌┐└┘├┤┬┴┼─╔╗╚╝║═")


GENERIC = (
    "process completed with exit code",
    "the process '/usr/bin/",
    "error: process completed",
    "the operation was canceled",
)


def _ci_signature(log, workflow=""):
    """The strongest failing line, normalised.

    Uses ci_diagnose.failing_lines rather than a fresh regex, because that
    function already lost the fight this one would lose: this repo's suites print
    "PASS  a PASSING security check is not diagnosed as a failure", so grepping
    for /fail|error/ signs a failure with the text of a passing assertion. Its
    ranking drops those. Reusing it also means one place decides what a failure
    line looks like.
    """
    try:
        import ci_diagnose                                  # noqa: WPS433
        ranked = ci_diagnose.failing_lines(log, limit=6)
    except Exception:                                       # noqa: BLE001
        ranked = [l for l in log.splitlines() if "##[error]" in l.lower()][:6]
    best = None
    for ln in ranked:
        if TABLE_CHARS & set(ln):
            continue                  # a table row is layout, not a diagnosis
        n = normalise(ln)
        if len(n) <= 12:
            continue
        if any(g in n for g in GENERIC):
            # QUALIFY THE FALLBACK BY WORKFLOW. Unqualified, "process completed
            # with exit code <n>" is the same string for every red run in the
            # repo, so one disposition would prefix-match all of them and mark
            # unrelated future failures as fixed — the exact false-fixed bug that
            # made matching a prefix in the first place. With the workflow name
            # in front it is still coarse, but it is coarse about ONE lane.
            best = best or ("ci:" + (workflow + ":" if workflow else "") + n,
                            str(ln).strip()[-200:])
            continue          # keep looking for something that identifies THIS failure
        return "ci:" + n, str(ln).strip()[-200:]
    return best or (None, "")


def source_router(audit=None):
    """Blocked decisions from the audit ledger, bucketed by the SAME normaliser
    the status page uses. Sharing it is the point: two views of one taxonomy
    cannot disagree, and that function is already the one that keeps prompts out
    of a label (finding D3)."""
    audit = audit or AUDIT
    if not os.path.isfile(audit):
        return [], "no audit ledger on this host; router failures unavailable"
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import status_data                                  # noqa: WPS433
    except Exception as e:                                  # noqa: BLE001
        return [], f"cannot import status_data: {e}"
    recs = []
    with open(audit, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or " " not in line:
                continue
            chain, _, blob = line.partition(" ")
            try:
                r = json.loads(blob)
            except ValueError:
                continue
            if not r.get("blocked"):
                continue
            cat = status_data.block_category(r.get("blocked"))
            if cat in POLICY_REFUSALS:
                continue
            recs.append({"ts": r.get("ts") or now().isoformat(), "source": "router",
                         "oid": "router:" + chain[:16], "signature": "router:" + cat,
                         "detail": cat, "context": str(r.get("scope") or "")})
    return recs, None


def dotenv(keys, path=None):
    """Fill named vars from .env when the environment does not already have them.

    WHY THIS IS NEEDED AT ALL. The daily collect runs from the Task Scheduler,
    which starts with a bare environment — so a key sitting in .env, where every
    other part of this stack reads its config, would never reach it. The source
    would go on reporting UNAVAILABLE with the credential right there on disk.

    Narrow on purpose: only the names asked for, only when unset, and the values
    are never printed. This is not a general dotenv loader and should not become
    one — a script that silently absorbs a whole secrets file is a different and
    worse thing than one that reads two documented keys.

    HOST vs CONTAINER: .env's N8N_API_URL is commented as `http://n8n:5678/...`,
    which resolves only INSIDE the compose network. An empty value is therefore
    left empty here so the host default (localhost) wins, rather than being
    overwritten with a hostname the host cannot resolve.
    """
    path = path or os.path.join(ROOT, ".env")
    if not os.path.isfile(path):
        return {}
    got = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k in keys and v and not os.environ.get(k):
                    os.environ[k] = v
                    got[k] = True
    except OSError:
        return {}
    return got


def _n8n_get(path, key, url, timeout=15):
    import urllib.error, urllib.request                     # noqa: WPS433
    req = urllib.request.Request(url.rstrip("/") + path, headers={"X-N8N-API-KEY": key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def source_lane(limit=LANE_LIMIT):
    """n8n execution failures, from the public API.

    Needs the API key; without it this source is UNAVAILABLE rather than empty,
    because an unreadable source that reports zero is indistinguishable from a
    quiet week.

    SIGNED ON workflowId, NOT THE NAME. The first version read
    `ex.workflowData.name` — a field the public API's `execution` schema does not
    have, so every signature would have silently fallen through to the id anyway.
    Checked against the spec the running instance serves at /api/v1/openapi.yml
    rather than against memory. The id turns out to be the better key regardless:
    ids here are stable and readable (`aicp-*`), while a name is display text
    somebody renames, which would split one recurrence into two. The name is
    fetched once per workflow and carried as DETAIL, where a rename costs nothing.

    NO CURSOR LOOP, and the cap is reported. `limit` maxes at 250 server-side;
    paging the whole history on every collect to find failures we already recorded
    would be a lot of requests for nothing, since the occurrence log is
    append-only and keeps what earlier runs saw. What that costs is discovery of
    failures older than the newest `limit` errors — said out loud, per the rule
    that a silent cap reads as "nothing older failed".
    """
    dotenv(("N8N_API_KEY", "N8N_API_URL"))
    key = os.environ.get("N8N_API_KEY", "")
    url = os.environ.get("N8N_API_URL") or "http://localhost:5678/api/v1"
    if not key:
        return [], ("N8N_API_KEY not set; lane execution history unavailable "
                    "(n8n UI -> Settings -> n8n API -> Create an API key)")
    import urllib.error                                     # noqa: WPS433
    try:
        data = _n8n_get(f"/executions?status=error&limit={int(limit)}", key, url)
    except urllib.error.HTTPError as e:
        # 401 is a wrong/expired key, not an outage, and the two need different
        # fixes. Saying "unreachable" for a bad credential sends you to the wrong
        # place for as long as you believe it.
        why = ("N8N_API_KEY rejected (401) — expired or from another instance"
               if e.code == 401 else f"n8n API returned HTTP {e.code}")
        return [], why
    except (urllib.error.URLError, OSError, ValueError) as e:
        return [], f"n8n API unreachable: {type(e).__name__}"

    names, recs = {}, []
    for ex in data.get("data", []):
        wid = str(ex.get("workflowId") or "?")
        if wid not in names:
            try:
                names[wid] = str(_n8n_get(f"/workflows/{wid}", key, url).get("name") or wid)
            except Exception:                               # noqa: BLE001
                names[wid] = wid                            # a deleted workflow still counts
        recs.append({"ts": ex.get("startedAt") or now().isoformat(), "source": "lane",
                     "oid": f"lane:{ex.get('id')}", "signature": f"lane:{wid}:error",
                     "detail": f"{names[wid]} — execution {ex.get('id')} errored",
                     "context": names[wid]})
    return recs, None


SOURCES = {"ci": source_ci, "router": source_router, "lane": source_lane}

# A REFUSAL IS NOT A FAULT, and the first version of this file got that wrong.
# It counted "unknown scope", "retired scope" and "cross-vendor blocked" as
# recurring incidents — 10 of them — when every one was the router correctly
# refusing a call. That is the control working, and a register that escalates it
# demands a "harness fix" for behaviour that is already correct, which is how a
# gate earns its reputation for crying wolf in the first week.
#
# The dividing question is not "was the request stopped" but "did the system do
# its job". These all did.
POLICY_REFUSALS = frozenset({
    "halted", "approval-required", "approval-unauthorized", "circuit-open",
    "mode-forbids-action", "PII refused", "unknown scope", "retired scope",
    "cross-vendor blocked", "scope not mapped",
})


# ------------------------------------------------------------------- register

def load_register(path=None):
    path = path or REGISTER
    if not os.path.isfile(path):
        return {"meta": {}, "entries": []}, [f"register not found at {path}"]
    try:
        import yaml                                         # noqa: WPS433
    except ImportError:
        return {"meta": {}, "entries": []}, ["pyyaml is not installed"]
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return {"meta": data.get("meta") or {},
            "entries": data.get("entries") or []}, []


def entry_for(signature, entries):
    """First entry whose `match` is a PREFIX of the signature.

    Prefix, not equality, because an error message eventually picks up a trailing
    clause and the register should survive that without a silent miss.

    Prefix, not substring, because substring got it wrong in the worst possible
    direction. A CI run failed with the recurrence gate's own output in its log —
    `- 'router:runner unreachable': proof failed ...` — so the signature CONTAINED
    a register entry's match string and inherited its `harness-fixed` status. An
    unrelated failure was reported as fixed. Signatures are namespaced
    (`ci:cause:<id>`, `router:<category>`, `ci:<message>`), so a prefix is both
    precise and enough.
    """
    sig = signature.lower()
    for e in entries:
        m = str(e.get("match", "")).lower()
        if m and sig.startswith(m):
            return e
    return None


IN_CI = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))


def run_assertion(asserted_by):
    """Command must exit zero AND print the expected substring.

    Returns (state, why) where state is ok / fail / unverified / na. The `requires`
    seam is copied from tests/findings_assert_test.py, along with its reasoning:
    node lives only in the containers here but exists in CI, so a proof needing
    it must be UNVERIFIED locally and a hard failure in CI. Passing an unrunnable
    proof is a loophole; failing every local run trains people to ignore the gate.
    """
    cmd = asserted_by.get("cmd") or ""
    expect = str(asserted_by.get("expect") or "")
    need = asserted_by.get("requires")
    if not cmd or not expect:
        return "fail", "asserted_by needs both cmd and expect"
    # `skip_if_absent` is for a proof about a file THIS TREE does not contain,
    # which is a different thing from `requires` (a runtime this MACHINE lacks).
    # The public snapshot publishes by allowlist, so a gate over an excluded file
    # has nothing to check there and its proof is not applicable — reported, never
    # a pass. Found when the first snapshot after publishing was re-enabled failed
    # the published repo's CI on the dependabot proof while this repo was green.
    absent = asserted_by.get("skip_if_absent")
    if absent and not os.path.exists(os.path.join(ROOT, *str(absent).split("/"))):
        return "na", f"{absent} is not in this tree — the proof cannot apply here"
    if need and not shutil.which(need) and not IN_CI:
        return "unverified", f"needs {need!r}, absent here — CI verifies this"
    try:
        r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return "fail", f"could not run: {type(e).__name__}"
    text = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        low = text.lower()
        if "not found" in low or "not recognized" in low or "no such file" in low:
            return "fail", "COMMAND COULD NOT RUN here — a claim nobody can check"
        return "fail", f"exit {r.returncode} — the harness fix is failing, not missing"
    if expect not in text:
        return "fail", f"ran clean but never printed {expect!r}"
    return "ok", "ok"


def check_shape(reg, unverified=None):
    """What CI can see: is the register itself honest? Runs the proofs."""
    problems = []
    unverified = unverified if unverified is not None else []
    seen_match = {}
    for e in reg["entries"]:
        sig = e.get("match") or "<no match>"
        if not e.get("match"):
            problems.append(f"{e.get('title', '?')}: no `match` — nothing can hit it")
        if e.get("match") in seen_match:
            problems.append(f"{sig!r}: duplicate `match`; the first entry always wins")
        seen_match[e.get("match")] = True
        status = e.get("status")
        if status not in STATUSES:
            problems.append(f"{sig!r}: status {status!r} is not one of {STATUSES}")
            continue
        if status == "harness-fixed":
            ab = e.get("asserted_by")
            if not isinstance(ab, dict):
                problems.append(f"{sig!r}: harness-fixed with no asserted_by — "
                                "that is a claim, not a fix")
                continue
            state, why = run_assertion(ab)
            if state == "fail":
                problems.append(f"{sig!r}: proof failed — {why}  [{ab.get('cmd')}]")
            elif state in ("unverified", "na"):
                unverified.append(f"{sig!r}: {why}")
        else:
            if not e.get("why"):
                problems.append(f"{sig!r}: {status} needs `why`")
            until = str(e.get("until") or "")
            if not until:
                problems.append(f"{sig!r}: {status} needs `until` — a disposition "
                                "with no expiry is how a register becomes a graveyard")
            elif until < today().isoformat():
                problems.append(f"{sig!r}: {status} EXPIRED on {until} — re-decide it")
    return problems


def check_counts(reg, occurrences, threshold=THRESHOLD):
    """The counting half: a pattern with no disposition fails."""
    problems = []
    for sig, agg in counted(occurrences).items():
        if agg["count"] < threshold:
            continue
        e = entry_for(sig, reg["entries"])
        if not e:
            problems.append(
                f"seen {agg['count']}x and undispositioned: {sig!r}\n"
                f"       first {agg['first'][:10]}, last {agg['last'][:10]}, "
                f"via {'+'.join(agg['sources'])}\n"
                f"       -> fix the harness, or file it in {rel(REGISTER)}")
    return problems


# --------------------------------------------------------------------- output

def cmd_collect(args):
    all_recs, notes = [], []
    for name in (args.source or list(SOURCES)):
        fn = SOURCES.get(name)
        if not fn:
            notes.append(f"{name}: no such source")
            continue
        recs, why = fn() if name != "ci" else fn(repo=args.repo, limit=args.limit)
        if why:
            notes.append(f"{name}: UNAVAILABLE — {why}")
        else:
            notes.append(f"{name}: {len(recs)} failure(s) seen" +
                         (f" (of the last {args.limit} runs — older failures are already"
                          " in the log, but nothing beyond that window is newly"
                          " discovered)" if name == "ci" else
                          f" (of the last {LANE_LIMIT} errored executions — same"
                          " discovery bound)" if name == "lane" else ""))
        all_recs += recs
    fresh = [] if args.dry_run else append(all_recs)
    for n in notes:
        print("  " + n)
    print(f"  {len(all_recs)} collected, {len(fresh)} new "
          f"({'DRY RUN, nothing written' if args.dry_run else rel(LOG)})")
    return 0


def cmd_check(args):
    reg, errs = load_register(args.register)
    unverified = []
    problems = list(errs) + check_shape(reg, unverified)
    fixed = sum(1 for e in reg["entries"] if e.get("status") == "harness-fixed")
    print(f"register: {len(reg['entries'])} entry(ies), {fixed} claimed harness-fixed, "
          f"{fixed - len(unverified)} proven here")

    if not args.shape:
        occ = read_log(args.log, args.days)
        if occ is None:
            # Not a pass. The whole point of this file is that silence is not
            # evidence, and that applies to the gate itself.
            print(f"  UNKNOWN — no occurrence log at {rel(args.log or LOG)}. "
                  "Run `recurrence.py collect` where the data lives.")
            print("  (shape checks below still ran)")
        else:
            print(f"  {len(occ)} occurrence(s) in {args.days}d, "
                  f"{len(counted(occ))} distinct signature(s)")
            problems += check_counts(reg, occ, args.threshold)

    for u in unverified:
        print("  UNVERIFIED  " + u)
    if problems:
        print("\nFAIL")
        for p in problems:
            print("  - " + p)
        return 1
    # Do not claim what was not checked — the same overclaim this file exists to
    # stop, made by the file.
    print("PASS recurrence register"
          + (f" ({len(unverified)} proof(s) UNVERIFIED on this machine)" if unverified else ""))
    return 0


def expiring(reg, within_days=14):
    """Dispositions that have run out, or are about to.

    A `watching`/`accepted` entry is a promise to look again on a date. Nobody
    keeps a promise they are never reminded of, so the review lane says these out
    loud BEFORE they turn into a red gate — which is the difference between a
    prompt and an ambush.
    """
    out = []
    soon = (today() + _dt.timedelta(days=within_days)).isoformat()
    for e in reg["entries"]:
        until = str(e.get("until") or "")
        if not until or e.get("status") == "harness-fixed":
            continue
        if until <= soon:
            out.append({"match": e.get("match"), "title": e.get("title", ""),
                        "status": e.get("status"), "until": until,
                        "expired": until < today().isoformat(),
                        "next_action": (e.get("next_action") or "").strip()})
    return sorted(out, key=lambda r: r["until"])


def build(register=None, log=None, days=WINDOW_DAYS, threshold=THRESHOLD, live=True,
          audit=None):
    """The report payload. Shared by the CLI and the lanes service, so the weekly
    message and the terminal cannot describe the same week differently.

    `live` recomputes the ROUTER source in memory from the mounted ledger instead
    of trusting a persisted snapshot. That source has a durable store of its own —
    the ledger IS the record — so re-deriving it costs nothing and means a lane
    with no write access still reports current router failures. The CI source has
    no such store here (it lives in GitHub, behind `gh` and a credential), so it
    is only as fresh as the last `collect`, and `sources` below says so rather
    than letting stale look like quiet.
    """
    reg, errs = load_register(register)
    stored = read_log(log, days)
    occ = list(stored or [])
    notes = {}
    if live:
        fresh, why = source_router(audit)
        notes["router"] = why or f"recomputed live from the ledger ({len(fresh)} fault(s))"
        if not why:
            seen = {r.get("oid") for r in occ}
            cutoff = (now() - _dt.timedelta(days=days)).isoformat()
            occ += [r for r in fresh
                    if r.get("oid") not in seen and str(r.get("ts", "")) >= cutoff]

    # Freshness per source, because "no CI failures this week" and "nobody has run
    # collect since April" produce the identical empty list.
    #
    # NEVER-COLLECTED IS THE EASY CASE. The one that hides is STOPPED: a source
    # collected happily for a month, the scheduled task died, and the note still
    # reads "last seen 2026-07-01" — true, unalarming, and quietly wrong for as
    # long as nobody does the subtraction. The staleness word is what makes the
    # weekly message a watchdog on the collector itself rather than only on the
    # failures it collects.
    for name in ("ci", "lane"):
        ts = [r["ts"] for r in occ if r.get("source") == name and r.get("ts")]
        if not ts:
            notes[name] = "nothing recorded — has `recurrence.py collect` run?"
            continue
        newest = max(ts)
        age = (now() - _dt.datetime.fromisoformat(newest)).days
        notes[name] = (f"STALE — last seen {newest[:10]} ({age}d ago); has collect stopped?"
                       if age > STALE_DAYS else f"last seen {newest[:10]} ({age}d ago)")

    rows = []
    for sig, a in counted(occ).items():
        e = entry_for(sig, reg["entries"])
        rows.append({"signature": sig, "count": a["count"], "first": a["first"],
                     "last": a["last"], "sources": a["sources"], "detail": a["detail"],
                     "status": (e or {}).get("status", "undispositioned"),
                     "title": (e or {}).get("title", "")})
    debt = [r for r in rows if r["count"] >= threshold and r["status"] == "undispositioned"]
    due = expiring(reg)
    return {
        "generated": now().isoformat(timespec="seconds"),
        "windowDays": days, "threshold": threshold,
        "haveData": stored is not None or bool(occ),
        "registerEntries": len(reg["entries"]),
        "registerErrors": errs,
        "sources": notes,
        "rows": rows, "harnessDebt": debt, "dispositionsDue": due,
    }


def cmd_sources(args):
    """Which sources can be read RIGHT NOW, and why not.

    Exists because "the credential is in place" and "the credential works" are
    different claims, and the gap between them is invisible in a report: an
    unavailable source and a quiet one both contribute nothing. This answers the
    question directly, without writing anything, so it is the safe thing to run
    after pasting a key.
    """
    ok = True
    for name in sorted(SOURCES):
        recs, why = (SOURCES[name](repo=args.repo) if name == "ci" else SOURCES[name]())
        if why:
            ok = False
            print(f"  UNAVAILABLE  {name}: {why}")
        else:
            print(f"  ok           {name}: readable, {len(recs)} failure(s) visible")
    print("\n" + ("every source is readable" if ok else
                  "at least one source is blind — it will report zero, which is "
                  "indistinguishable\nfrom a quiet week until you fix it"))
    return 0 if ok else 1


def cmd_report(args):
    p = build(args.register, args.log, args.days, args.threshold, live=not args.no_live)
    if args.as_json:
        print(json.dumps(p, indent=2))
        return 1 if p["harnessDebt"] else 0
    if not p["rows"] and not p["haveData"]:
        print("no occurrence log on this host — nothing counted")
        return 0
    total = sum(r["count"] for r in p["rows"])
    print(f"Recurrence — {total} occurrence(s) over {args.days} days\n")
    for r in p["rows"]:
        flag = "!!" if r in p["harnessDebt"] else ("  " if r["count"] < args.threshold else "ok")
        print(f"  {flag} {r['count']:>3}x  {r['status']:<16} {r['signature'][:80]}")
    for name, note in sorted(p["sources"].items()):
        print(f"       {name}: {note}")
    for d in p["dispositionsDue"]:
        print(f"  {'!!' if d['expired'] else '..'} {'EXPIRED' if d['expired'] else 'due'} "
              f"{d['until']}  {d['status']}  {str(d['match'])[:60]}")
    if p["harnessDebt"]:
        print(f"\n{len(p['harnessDebt'])} pattern(s) need a harness fix or a disposition.")
    return 1 if p["harnessDebt"] else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--register", default=REGISTER)
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--threshold", type=int, default=THRESHOLD)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="append failures from the live sources")
    c.add_argument("--source", action="append", choices=sorted(SOURCES))
    c.add_argument("--repo", default=os.environ.get("AICP_GH_REPO", ""))
    c.add_argument("--dry-run", action="store_true")
    # Stated, not hidden: `gh run list` pages, and a cap that silently drops the
    # tail reads as "nothing older failed". The log is append-only so previously
    # collected occurrences survive; this bounds DISCOVERY, and cmd_collect says so.
    c.add_argument("--limit", type=int, default=60,
                   help="how many recent GitHub Actions runs to inspect")

    k = sub.add_parser("check", help="the gate")
    k.add_argument("--shape", action="store_true",
                   help="register honesty only — the mode CI can actually run")

    sc = sub.add_parser("sources", help="can each source actually be read right now?")
    sc.add_argument("--repo", default=os.environ.get("AICP_GH_REPO", ""))

    r = sub.add_parser("report", help="what is recurring")
    r.add_argument("--json", action="store_true", dest="as_json")
    r.add_argument("--no-live", action="store_true",
                   help="report only the persisted log; do not re-derive the "
                        "router source from the ledger")

    args = ap.parse_args(argv)
    return {"collect": cmd_collect, "check": cmd_check, "report": cmd_report,
            "sources": cmd_sources}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
