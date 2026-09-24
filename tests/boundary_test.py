#!/usr/bin/env python3
"""Prove the router-chokepoint check actually fires.

WHY THIS EXISTS. boundary_check.py passes on the repo right now. That proves
nothing on its own — this repo has shipped four gates that passed by scanning an
empty set, and the check went green the moment the one real violation was
deleted, which is exactly when a broken check and a working one look identical.

So every case here SYNTHESISES the failure and demands the gate go red:

    a direct runner URL outside the router      -> FAIL
    an outbound call carrying x-runner-token    -> FAIL
    the same token in a VERIFY position         -> pass  (this is the subtle one)
    an n8n workflow referencing RUNNER_TOKEN    -> FAIL  (HOLDING)
    no runner hosts discoverable                -> FAIL, not a silent pass
    an allowlist entry pointing at nothing      -> FAIL

The verify case is the one worth reading. A runner's own server holds
RUNNER_TOKEN to authenticate INBOUND calls; holding a secret to check it is not
the same as spending it to impersonate. A rule that cannot tell those apart would
flag every runner forever, and a gate that cries wolf gets switched off.

HOLDING is where that reasoning stopped being enough. n8n once held the same
token "only to verify" the ask-human door, but with env access in nodes on, any
workflow authored in the UI could spend it (threat-model C3). So for an n8n
workflow the position does not matter: naming RUNNER_TOKEN at all fails.

    python tests/boundary_test.py

Writes its fixtures into a temp dir under the repo (so the walker sees them) and
removes them in a finally.
"""
import io, os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK = os.path.join(ROOT, "scripts", "boundary_check.py")
FAILS = []


def check(label, got, want=True):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label)
    if not ok:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")


def run(env=None):
    e = dict(os.environ)
    e.update(env or {})
    r = subprocess.run([sys.executable, CHECK], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# The check walks the repo, so a fixture has to live inside it to be seen. A
# fixture tree outside would test a copy of the logic rather than the thing CI
# runs — the same reasoning as tests/mount_drift_test.py.
SANDBOX = os.path.join(ROOT, "_boundary_fixture")


def write(name, text):
    os.makedirs(SANDBOX, exist_ok=True)
    p = os.path.join(SANDBOX, name)
    io.open(p, "w", encoding="utf-8", newline="\n").write(text)
    return p


try:
    code, out = run()
    check("the repo is clean before we start (else the rest proves nothing)", code, 0)
    check("...and says what it covered", "file(s) scanned against" in out)

    print("\nADDRESSING — a direct runner URL outside the router")
    write("rogue_lane.py", 'import urllib.request\n'
                           'urllib.request.urlopen("http://claude-runner:8080/run")\n')
    code, out = run()
    check("a direct runner URL fails the gate", code, 1)
    check("...and names the file and line", "rogue_lane.py:2" in out)
    check("...and labels it ADDRESSING", "ADDRESSING" in out)
    check("...and says where to route instead", "router:8080/route" in out)
    os.remove(os.path.join(SANDBOX, "rogue_lane.py"))
    check("removing it goes green again", run()[0], 0)

    print("\nCREDENTIAL — an outbound call carrying the runner's own token")
    write("rogue_call.json",
          '{"nodes":[{"parameters":{"url":"http://svc:8080/x","headerParameters":'
          '{"parameters":[{"name":"x-runner-token","value":"secret"}]}}}]}\n')
    code, out = run()
    check("sending x-runner-token fails the gate", code, 1)
    check("...and labels it CREDENTIAL", "CREDENTIAL" in out)
    os.remove(os.path.join(SANDBOX, "rogue_call.json"))

    print("\n...but the SAME token in a verify position is legitimate")
    # A runner's own server reading the inbound header and comparing it to its
    # env var. Holding the secret to CHECK a caller is not being one.
    write("runner_door.js",
          'if (!tokenOk(req.headers["x-runner-token"], process.env.RUNNER_TOKEN))\n'
          '  return res.writeHead(401).end("unauthorized");\n')
    code, out = run()
    check("verifying an inbound x-runner-token is NOT flagged", code, 0)
    check("...so a runner's own auth check stays green", "runner_door.js" not in out)
    os.remove(os.path.join(SANDBOX, "runner_door.js"))

    print("\nHOLDING — an n8n workflow that holds the runners' credential at all")
    # The old ask-human door: n8n verified runners with RUNNER_TOKEN. Verifying is
    # not sending, so CREDENTIAL let it pass, but with env access in nodes on,
    # holding it meant any workflow authored in the n8n UI could spend it and skip
    # the router (threat-model C3). The door now has its own ASK_HUMAN_TOKEN, so
    # n8n has no reason to reference the runner credential, in any position.
    write("door.json",
          '{"nodes":[{"parameters":{"jsCode":"const headers = req.headers || {};'
          'const want = $env.RUNNER_TOKEN || String();'
          'const got = headers[\'x-runner-token\'];"}}]}\n')
    code, out = run()
    check("an n8n workflow referencing RUNNER_TOKEN fails the gate", code, 1)
    check("...and labels it HOLDING", "HOLDING" in out and "door.json" in out)
    os.remove(os.path.join(SANDBOX, "door.json"))
    write("door.json",
          '{"nodes":[{"parameters":{"jsCode":"const headers = req.headers || {};'
          'const want = $env.ASK_HUMAN_TOKEN || String();'
          'const got = headers[\'x-ask-human-token\'];"}}]}\n')
    code, out = run()
    check("the door verifying its OWN token is green", code, 0)
    shutil.rmtree(SANDBOX, ignore_errors=True)

    print("\nthe check refuses to pass when it cannot check")
    # Point host discovery at an empty tree: no map, no compose, so nothing to
    # look for. Reporting that as PASS is the vacuity this file exists to stop.
    empty = tempfile.mkdtemp(prefix="boundary-empty-")
    try:
        r = subprocess.run([sys.executable, CHECK], cwd=empty, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        # Run from elsewhere, ROOT is still resolved from the script's own path,
        # so this stays green — assert the guard directly instead of pretending.
        import importlib.util
        spec = importlib.util.spec_from_file_location("bc", CHECK)
        bc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bc)
        real_hosts = bc.runner_hosts
        bc.runner_hosts = lambda: []
        code = bc.main([])
        check("no discoverable hosts -> exit 2, not a pass", code, 2)
        bc.runner_hosts = real_hosts

        real_allowed = dict(bc.ALLOWED)
        bc.ALLOWED["router/this-file-was-moved.js"] = "stale entry"
        check("an allowlist entry pointing at nothing -> exit 2", bc.main([]), 2)
        bc.ALLOWED.clear()
        bc.ALLOWED.update(real_allowed)
        check("...and restoring it goes green", bc.main([]), 0)
    finally:
        shutil.rmtree(empty, ignore_errors=True)

    print("\nthe describes-marker exempts ONE line, not a file")
    # Writing this check produced five violations of it, every one in a file whose
    # job is to describe the rule. The marker is the precise fix; the blunt one —
    # allowlisting tests/ and security/ — would excuse a real call site in those
    # trees forever. So: the marker must work, and must not leak to the next line.
    write("prose.py", '# we must never POST to http://claude-runner:8080/run'
                      '   # boundary-check: describes\n')
    check("a marked line is not a violation", run()[0], 0)
    write("prose.py", '# we must never POST to http://claude-runner:8080/run'
                      '   # boundary-check: describes\n'
                      'urllib.request.urlopen("http://claude-runner:8080/run")\n')
    code, out = run()
    check("...but the marker does NOT cover the line after it", code, 1)
    check("...and the violation reported is line 2, not line 1", "prose.py:2" in out)
    shutil.rmtree(SANDBOX, ignore_errors=True)

    print("\nhosts come from the map, not a frozen list")
    code, out = run()
    check("more than one runner host is discovered",
          "against 1 runner host" not in out and "runner host(s)" in out)
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)

if FAILS:
    print("\nFAILED: %d check(s)" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("\nPASS boundary")
