#!/usr/bin/env python3
"""The router is the only thing that may reach a runner. Enforced, not asserted.

WHY THIS EXISTS. "Route through the router, never straight to a runner" is the
chokepoint the whole design rests on — it is where tiering, the audit ledger, the
kill switch, PII policy and cross-vendor blocking all live. Anything that talks
to a runner directly gets the agent and none of the governance. It is finding C2
in the threat model, and until now it was enforced by prose and code review.

IT IS NOT AN IMPORT RULE, which is why no architecture-conformance tool could
catch it. There is no import to forbid — the violation is a STRING,
`http://claude-runner:8080/run`, in an HTTP call. ArchUnit, import-linter and
dependency-cruiser all reason over module graphs and would report this repo clean
while a workflow bypassed the router entirely. The full build-vs-buy survey,
with maintenance data and what this gives up, is
docs/decisions/0017-architectural-drift-tooling.md.

THREE RULES, because there are three ways to end up able to reach a runner:

  ADDRESSING   a URL naming a runner host, anywhere but the router.
  CREDENTIAL   an `x-runner-token` header in a SEND position — an outbound call
               carrying the runner's own credential.
  HOLDING      an n8n workflow that references RUNNER_TOKEN at all.

The send/verify distinction in the second rule is load-bearing. A runner's own
server reads the header to authenticate its caller; holding the secret to check
it is not the same as spending it to impersonate, so CREDENTIAL flags the header
only where it is SENT.

HOLDING exists because that distinction was not enough for n8n. n8n used to hold
RUNNER_TOKEN to verify the ask-human door, which CREDENTIAL correctly passed. But
env access in nodes is on, so any workflow authored in the n8n UI could read the
same variable and spend it, skipping the router (threat-model C3). The door now
has its own ASK_HUMAN_TOKEN, so an n8n workflow has no legitimate reason to name
the runner credential in any position, and this rule makes that permanent.

    python scripts/boundary_check.py
    python scripts/boundary_check.py --list-hosts

Hosts are discovered from context/runner-map.json and the generated compose, not
hardcoded. A hardcoded list is itself a drift risk: add a sixth runner and a
frozen list keeps passing while the new one is unguarded.

Stdlib + pyyaml.
"""
from __future__ import annotations
import argparse, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files that may name a runner host, each for a stated reason. Naming a service
# is not calling it — the distinction this list encodes is DECLARING vs DIALLING.
ALLOWED = {
    "router/server.js":
        "the dispatcher itself — this is the authority the rule exists to concentrate",
    "context/runner-map.json":
        "the router's own configuration data, read by the router",
    "docker-compose.yml":
        "service definitions; declaring a service is not calling it",
    "compose.scopes.yml":
        "generated service definitions, same reason",
    "docker-compose.wsl.yml":
        "host-specific overrides over those definitions",
    "scripts/gen_compose.py":
        "generates the definitions above",
    "scripts/boundary_check.py":
        "this file names the pattern in order to look for it",
    "tests/boundary_test.py":
        "it must contain a real violation in order to prove this check fires; "
        "the fourth time this repo has had to say that our vocabulary for "
        "DESCRIBING a failure collides with the failure itself",
}
# Prose may describe the topology freely; documentation is not a call site.
ALLOWED_PREFIXES = ("docs/", "idea-dossier/", "marketing/", "README.md", "AGENTS.md")

# A line-level opt-out, because whole-file allowlisting is too blunt for this.
#
# Writing this check produced five separate violations of it, all in files whose
# job is to DESCRIBE the rule: the check itself, its test, the comment in
# security_test.py explaining why that check missed one, and twice in the C2
# finding note. Our vocabulary for describing a failure IS the failure, which
# this repo has now hit in four different controls.
#
# The blunt fix — allowlist tests/ and security/ — would excuse a real call site
# in those trees forever. The marker is precise instead: it exempts ONE line, it
# is visible at the line it exempts, and it cannot silently widen.
MARKER = "boundary-check: describes"

TEXT_EXT = (".js", ".mjs", ".py", ".json", ".yml", ".yaml", ".sh", ".ps1", ".cmd", ".md")
SKIP_DIRS = {".git", "node_modules", "__pycache__", "audit", "vault", "generated",
             "eval", "workspace", "control", ".engineering-standard"}


def runner_hosts():
    """Every hostname that answers as a runner, from the map and the compose."""
    hosts = set()
    mp = os.path.join(ROOT, "context", "runner-map.json")
    if os.path.isfile(mp):
        try:
            m = json.load(open(mp, encoding="utf-8"))
            hosts |= set((m.get("scopeToHost") or {}).values())
            hosts |= {h for h in (m.get("providerHosts") or {}).values() if h}
        except (OSError, ValueError):
            pass
    try:
        import yaml                                          # noqa: WPS433
        for f in ("docker-compose.yml", "compose.scopes.yml"):
            p = os.path.join(ROOT, f)
            if not os.path.isfile(p):
                continue
            d = yaml.safe_load(open(p, encoding="utf-8")) or {}
            hosts |= {s for s in (d.get("services") or {}) if "runner" in s}
    except ImportError:
        pass
    return sorted(h for h in hosts if h)


def tracked_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(TEXT_EXT):
                yield os.path.relpath(os.path.join(base, f), ROOT).replace(os.sep, "/")


def is_n8n_workflow(text):
    try:
        d = json.loads(text)
    except ValueError:
        return False
    return isinstance(d, dict) and isinstance(d.get("nodes"), list)


def allowed(rel):
    return rel in ALLOWED or rel.startswith(ALLOWED_PREFIXES)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list-hosts", action="store_true")
    args = ap.parse_args(argv)

    hosts = runner_hosts()
    if args.list_hosts:
        print("\n".join(hosts) or "(none)")
        return 0

    # A check that found nothing to look for is checking nothing. This repo has
    # shipped four gates that passed by scanning an empty set; this one refuses.
    if not hosts:
        print("FAIL no runner hosts discovered from the runner map or compose.\n"
              "     Nothing could be checked, so this is not a pass — fix the\n"
              "     discovery before trusting the result.")
        return 2

    # A stale allowlist is a silent hole: the entry keeps excusing a path that no
    # longer exists while the real file moves somewhere unguarded.
    missing = [f for f in ALLOWED if not os.path.exists(os.path.join(ROOT, f))]
    if missing:
        print("FAIL the allowlist names files that do not exist: " + ", ".join(missing))
        return 2

    url = re.compile(r"https?://(" + "|".join(re.escape(h) for h in hosts) + r")\b", re.I)
    send = re.compile(r"x-runner-token", re.I)
    verify = re.compile(r"headers\[|req\.headers|process\.env\.RUNNER_TOKEN")
    holding = re.compile(r"RUNNER_TOKEN")

    addressing, credential, held, scanned = [], [], [], 0
    for rel in sorted(tracked_files()):
        try:
            text = open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        scanned += 1
        if allowed(rel):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if MARKER in line:
                continue
            m = url.search(line)
            if m:
                addressing.append((rel, i, m.group(0)))
        # SEND vs VERIFY. A file that mentions the header only where it also reads
        # an inbound request is checking a caller, not being one. Deliberately
        # coarse — it is a whole-file judgement — because the alternative is
        # parsing every n8n node type, and a coarse rule that a human reviews
        # beats a precise one nobody can read.
        if send.search(text) and not verify.search(text):
            for i, line in enumerate(text.splitlines(), 1):
                if send.search(line) and MARKER not in line:
                    credential.append((rel, i, "sends x-runner-token"))
                    break
        # HOLDING: an n8n workflow is a JSON file with a `nodes` list. Recognised
        # by shape rather than by directory, so a workflow saved outside
        # n8n-workflows/ is still covered.
        if rel.endswith(".json") and holding.search(text) and is_n8n_workflow(text):
            for i, line in enumerate(text.splitlines(), 1):
                if holding.search(line) and MARKER not in line:
                    held.append((rel, i, "n8n workflow references RUNNER_TOKEN"))
                    break

    for rel, i, what in addressing:
        print(f"  ADDRESSING  {rel}:{i}  {what}")
    for rel, i, what in credential:
        print(f"  CREDENTIAL  {rel}:{i}  {what}")
    for rel, i, what in held:
        print(f"  HOLDING     {rel}:{i}  {what}")

    n = len(addressing) + len(credential) + len(held)
    if n:
        print(f"\nFAIL {n} path(s) reach a runner without going through the router.\n"
              "     The router is where tiering, the audit ledger, the kill switch,\n"
              "     PII policy and cross-vendor blocking live. A direct call gets the\n"
              "     agent and none of them (threat model C2).\n"
              "     Route through http://router:8080/route, or add the file to\n"
              "     ALLOWED in this script WITH A REASON if it only declares.")
        if held:
            print("     HOLDING: n8n must not hold the runner credential (threat model\n"
                  "     C3). The ask-human door verifies ASK_HUMAN_TOKEN instead.")
        return 1
    print(f"PASS router chokepoint: {scanned} file(s) scanned against "
          f"{len(hosts)} runner host(s), {len(ALLOWED)} allowlisted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
