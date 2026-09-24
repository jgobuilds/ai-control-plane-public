#!/usr/bin/env python3
"""Prove the recurrence gate fires, and refuses the ways of lying to it.

WHY THIS EXISTS. The register passes right now, on the real repo, with every
signature dispositioned. That proves nothing at all — an empty check passes too,
and this repo has shipped four vacuous gates already. So every assertion here
SYNTHESISES the failure and demands the gate go red:

    a pattern nobody filed          -> FAIL
    a proof that does not prove     -> FAIL   (exit 0, wrong output)
    a disposition past its expiry   -> FAIL
    a single occurrence             -> pass   (once is not a pattern)
    a policy refusal                -> not even counted
    no occurrence log at all        -> UNKNOWN, never pass

Fixtures only — it writes its own register and occurrence log into a temp dir and
never reads the real ones, so it is the same result on a laptop and in CI.

    python tests/recurrence_test.py
"""
import io, json, os, shutil, sys, tempfile
import datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import recurrence as rc                                        # noqa: E402

FAILS = []


def check(label, got, want=True):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label)
    if not ok:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")


def occ(sig, day, oid=None, source="ci"):
    return {"ts": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=day)).isoformat(),
            "source": source, "oid": oid or f"{sig}:{day}", "signature": sig,
            "detail": "synthetic"}


def gate(tmp, entries, occurrences, shape_only=False):
    """Write a fixture register + log, run the gate, return (exit, output)."""
    reg = os.path.join(tmp, "register.yaml")
    log = os.path.join(tmp, "occurrences.jsonl")
    import yaml
    io.open(reg, "w", encoding="utf-8", newline="\n").write(
        yaml.safe_dump({"meta": {}, "entries": entries}, sort_keys=False))
    if occurrences is None:
        if os.path.exists(log):
            os.remove(log)
    else:
        with io.open(log, "w", encoding="utf-8", newline="\n") as fh:
            for o in occurrences:
                fh.write(json.dumps(o) + "\n")
    argv = ["check", "--register", reg, "--log", log]
    # Global options come before the subcommand.
    argv = ["--register", reg, "--log", log, "check"] + (["--shape"] if shape_only else [])
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rc.main(argv)
    return code, buf.getvalue()


tmp = tempfile.mkdtemp(prefix="recurrence-test-")
try:
    print("the doctrine — twice is a pattern, once is not")
    code, out = gate(tmp, [], [occ("ci:boom", 1), occ("ci:boom", 2)])
    check("2 occurrences, no entry -> FAIL", code, 1)
    check("...and it names the signature", "ci:boom" in out)
    code, out = gate(tmp, [], [occ("ci:boom", 1)])
    check("1 occurrence, no entry -> pass", code, 0)

    print("\na disposition has to actually dispose")
    FIXED = [{"match": "ci:boom", "title": "t", "status": "harness-fixed",
              "asserted_by": {"cmd": f'"{sys.executable}" -c "print(\'the proof ran\')"',
                              "expect": "the proof ran"}}]
    code, out = gate(tmp, FIXED, [occ("ci:boom", 1), occ("ci:boom", 2)])
    check("2 occurrences + a proof that passes -> pass", code, 0)

    # The one that matters: a command that exits 0 but proves nothing. This is how
    # a register rots — the entry stays green while the harness fix is deleted.
    LIES = [{"match": "ci:boom", "title": "t", "status": "harness-fixed",
             "asserted_by": {"cmd": f'"{sys.executable}" -c "print(\'unrelated\')"',
                             "expect": "the proof ran"}}]
    code, out = gate(tmp, LIES, [occ("ci:boom", 1), occ("ci:boom", 2)])
    check("exit 0 but the expected string is absent -> FAIL", code, 1)
    check("...and it says so, not 'command failed'", "never printed" in out)

    NOPROOF = [{"match": "ci:boom", "title": "t", "status": "harness-fixed"}]
    code, out = gate(tmp, NOPROOF, [occ("ci:boom", 1), occ("ci:boom", 2)])
    check("harness-fixed with no asserted_by -> FAIL", code, 1)

    print("\nexpiry — the only thing keeping the register off the graveyard path")
    past = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    soon = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    code, _ = gate(tmp, [{"match": "ci:boom", "title": "t", "status": "accepted",
                          "why": "known", "until": soon}], [occ("ci:boom", 1), occ("ci:boom", 2)])
    check("accepted, in date -> pass", code, 0)
    code, out = gate(tmp, [{"match": "ci:boom", "title": "t", "status": "accepted",
                            "why": "known", "until": past}], [occ("ci:boom", 1)])
    check("accepted, EXPIRED -> FAIL even with one occurrence", code, 1)
    check("...and it says re-decide it", "re-decide" in out)
    code, _ = gate(tmp, [{"match": "ci:boom", "title": "t", "status": "accepted",
                          "why": "known"}], [occ("ci:boom", 1)])
    check("accepted with no `until` at all -> FAIL", code, 1)

    print("\nno data is UNKNOWN, never pass")
    code, out = gate(tmp, [], None)
    check("missing occurrence log says UNKNOWN", "UNKNOWN" in out)
    check("...and does not print PASS on the count", "occurrence(s) in" not in out)

    print("\nnormalise — two runs of one failure are one signature")
    a = ("build\tstep\t2026-08-07T07:25:11.9482483Z Failure running container "
         "cebf2aac0fea5ede1601511e4f73153c: Error: Command failed with exit code 1")
    b = ("build\tstep\t2026-08-03T21:32:26.8571716Z Failure running container "
         "975a775c54aec93bfa6f79e92837602b: Error: Command failed with exit code 1")
    check("different container id + timestamp collapse", rc.normalise(a) == rc.normalise(b))
    check("...but a different error does not",
          rc.normalise(a) != rc.normalise(a.replace("Command failed", "Disk full")))
    check("the ##[error] marker is stripped, not signed",
          "##[error]" not in rc.normalise("##[error]Process completed with exit code 1"))

    print("\ncollect is idempotent — running it twice is not a pattern")
    log2 = os.path.join(tmp, "append.jsonl")
    one = [{"ts": "2026-08-07T00:00:00+00:00", "source": "ci", "oid": "ci:1",
            "signature": "ci:x", "detail": ""}]
    rc.append(one, log2)
    fresh = rc.append(one, log2)
    check("the second append writes nothing", fresh, [])
    check("...so the count stays 1", len(rc.read_log(log2)), 1)

    print("\n.env is read for the two n8n keys — narrowly, and only when unset")
    # The daily collect runs from the Task Scheduler with a bare environment, so
    # without this the key could sit in .env forever while the source kept
    # reporting UNAVAILABLE. Narrow on purpose: a script that absorbs a whole
    # secrets file is a different and worse thing than one reading two keys.
    envp = os.path.join(tmp, "dotenv-fixture")
    io.open(envp, "w", encoding="utf-8", newline="\n").write(
        '# a comment\nN8N_API_KEY="from-dotenv"\nN8N_API_URL=\nUNRELATED_SECRET=nope\n')
    for k in ("N8N_API_KEY", "N8N_API_URL", "UNRELATED_SECRET"):
        os.environ.pop(k, None)
    rc.dotenv(("N8N_API_KEY", "N8N_API_URL"), envp)
    check("the key is picked up, quotes stripped", os.environ.get("N8N_API_KEY"), "from-dotenv")
    check("an EMPTY value does not overwrite the host default",
          os.environ.get("N8N_API_URL"), None)
    check("a name not asked for is never absorbed",
          os.environ.get("UNRELATED_SECRET"), None)
    os.environ["N8N_API_KEY"] = "already-set"
    rc.dotenv(("N8N_API_KEY",), envp)
    check("an environment that already has it wins",
          os.environ.get("N8N_API_KEY"), "already-set")
    for k in ("N8N_API_KEY", "N8N_API_URL"):
        os.environ.pop(k, None)
    check("a missing .env is silent, not fatal", rc.dotenv(("X",), envp + ".nope"), {})

    print("\nthe lane source signs on workflowId, and says WHY it cannot read")
    # The first version read `ex.workflowData.name`. The public API's `execution`
    # schema has no such field (checked against the spec the running instance
    # serves, not against memory), so every signature would have fallen through to
    # the id in silence. The id is the better key anyway: ids here are stable,
    # names are display text somebody renames, and a rename would split one
    # recurrence into two.
    r, why = rc.source_lane()
    check("no key -> UNAVAILABLE, not empty", bool(why), True)
    check("...and it names the env var", "N8N_API_KEY" in why, True)
    check("...and says where to get one", "Settings" in why, True)
    check("...and returns no records", r, [])

    _real_get = rc._n8n_get
    try:
        rc._n8n_get = lambda path, key, url, timeout=15: (
            {"data": [{"id": "77", "workflowId": "aicp-eval-drift",
                       "startedAt": "2026-08-06T07:00:00.000Z", "status": "error"}]}
            if path.startswith("/executions") else {"name": "Eval / drift lane"})
        os.environ["N8N_API_KEY"] = "test-key"
        recs, why = rc.source_lane()
        check("an errored execution becomes one occurrence", len(recs), 1)
        check("signed on the workflow ID, not its name",
              recs[0]["signature"], "lane:aicp-eval-drift:error")
        check("the NAME rides along as detail, where a rename costs nothing",
              "Eval / drift lane" in recs[0]["detail"], True)
        check("the occurrence id is the execution id, so collect stays idempotent",
              recs[0]["oid"], "lane:77")
        check("no error reported", why, None)

        # A wrong key and a dead server need different fixes, so they get
        # different messages. Saying "unreachable" for a bad credential sends you
        # to the wrong place for as long as you believe it.
        import urllib.error

        def _401(path, key, url, timeout=15):
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        rc._n8n_get = _401
        _, why401 = rc.source_lane()
        check("401 is reported as a rejected key, not an outage",
              "rejected" in why401 and "401" in why401, True)
        check("...and is NOT called unreachable", "unreachable" in why401, False)
    finally:
        rc._n8n_get = _real_get
        os.environ.pop("N8N_API_KEY", None)

    print("\nmatching is PREFIX — a signature that merely quotes an entry is not that entry")
    # This is the failure that made it prefix. A CI run failed with the recurrence
    # gate's own output in its log, so the signature CONTAINED the string
    # "router:runner unreachable" and, under substring matching, inherited that
    # entry's harness-fixed status. An unrelated failure reported as fixed is the
    # worst error this file can make, so it gets its own case.
    ENTRIES = [{"match": "router:runner unreachable", "title": "t",
                "status": "accepted", "why": "w",
                "until": (dt.date.today() + dt.timedelta(days=30)).isoformat()}]
    quoting = "ci:- 'router:runner unreachable': proof failed - ran clean but never printed"
    check("the real signature still matches",
          (rc.entry_for("router:runner unreachable", ENTRIES) or {}).get("status"), "accepted")
    check("a signature with a trailing clause still matches",
          (rc.entry_for("router:runner unreachable (ENOTFOUND)", ENTRIES) or {}).get("status"),
          "accepted")
    check("a signature that merely QUOTES it does not", rc.entry_for(quoting, ENTRIES), None)
    code, out = gate(tmp, ENTRIES, [occ(quoting, 1), occ(quoting, 2)])
    check("...so it counts as undispositioned and fails the gate", code, 1)

    print("\nstaleness — a STOPPED collector must not look like a quiet fortnight")
    # The scheduled host-side collect can die. When it does the occurrence log
    # simply stops growing, and "last seen 2026-07-01" stays true, unalarming and
    # wrong for as long as nobody does the subtraction. This is the only thing
    # making the weekly lane a watchdog on the COLLECTOR rather than only on what
    # it collects, so it gets a test.
    stale_log = os.path.join(tmp, "stale.jsonl")
    fresh_log = os.path.join(tmp, "fresh.jsonl")
    for path, age in ((stale_log, rc.STALE_DAYS + 5), (fresh_log, 1)):
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(occ("ci:x", age, oid="ci:1")) + "\n")
    p_stale = rc.build(log=stale_log, live=False)
    p_fresh = rc.build(log=fresh_log, live=False)
    check("a source silent past STALE_DAYS says STALE",
          "STALE" in p_stale["sources"]["ci"], True)
    check("...and asks the question out loud",
          "has collect stopped?" in p_stale["sources"]["ci"], True)
    check("a fresh source does not", "STALE" in p_fresh["sources"]["ci"], False)
    check("never-collected keeps its own distinct message",
          "has `recurrence.py collect` run?" in p_stale["sources"]["lane"], True)

    print("\na refusal is not a fault")
    check("PII refused is excluded", "PII refused" in rc.POLICY_REFUSALS)
    check("cross-vendor blocked is excluded", "cross-vendor blocked" in rc.POLICY_REFUSALS)
    check("unknown scope is excluded", "unknown scope" in rc.POLICY_REFUSALS)
    check("a real fault is NOT excluded", "runner error" in rc.POLICY_REFUSALS, False)

    print("\nthe shipped register is itself well-formed")
    reg, errs = rc.load_register()
    check("it loads", errs, [])
    check("it has entries", len(reg["entries"]) > 0)
    for e in reg["entries"]:
        check(f"  {str(e.get('match'))[:44]!r} has a title", bool(e.get("title")))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if FAILS:
    print("\nFAILED: %d check(s)" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("\nPASS recurrence")
