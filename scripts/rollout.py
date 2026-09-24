#!/usr/bin/env python3
"""Roll changes out one runner first, verify, then promote — instead of recreating everything at once.

WHY THIS EXISTS. On 2026-09-17 the whole stack was rebuilt and recreated twice within
sixteen minutes (11:29Z and 11:45Z): every runner, the router, the scrubber and the
lanes at the same instant, with unpinned agent CLIs the first time. Nothing failed —
but nothing would have limited it if something had: a CLI release that broke tool
enforcement, or a runner contract the router did not match, would have reached every
scope together, with no step at which anyone looked.

This makes the rollout a sequence with a gate between steps:

    plan                     the waves, and what each service runs now
    verify [SERVICE...]      read-only: running, /health answers 200 within the
                             service's boot grace (n8n 60s, others 20s; polled, and a
                             crash ends the wait), CLI pins match the checkout,
                             optional live enforcement probe
    canary [--service S]     rebuild and recreate ONE runner (default
                             claude-runner-commons: pooled, lowest blast radius), then
                             verify it
    observe --since TS       the audit ledger since the canary started: errors
                             attributable to the canary fail it; NO traffic to the
                             canary is inconclusive, not a pass
    promote                  the remaining waves in order, verifying each service
                             before touching the next; stops at the first failure
    rollback SERVICE         put back the image the service ran before this tool
                             last recreated it

WAVES, callees before callers: canary -> other runners -> scrubber, lanes and other
built services -> router -> n8n. A runner contract change must be live before the
router starts relying on it, and n8n, which generates the traffic, moves last.

SAFETY — deploy only from the checkout that is running. Compose resolves relative
bind mounts (./audit, ./workspace) against the compose file's directory, so running
`docker compose up` from a DIFFERENT checkout (a git worktree, a second clone)
silently re-points production at that checkout's ledger and scope tree. Every
mutating command refuses unless this script's repo root is the working directory
recorded on the running containers. It also refuses a dirty tree (deploying code
that is not committed), unless --allow-dirty.

Recreation uses `up -d --no-deps` without --force-recreate, so a service whose image
and config did not change is left running — the tool reports which ones moved.

    python scripts/rollout.py plan
    python scripts/rollout.py verify --probe
    python scripts/rollout.py canary --probe
    python scripts/rollout.py observe --since 2026-09-17T12:00:00Z
    python scripts/rollout.py promote
    python scripts/rollout.py rollback claude-runner-commons

Rollback state lives in audit/rollout-state.json (gitignored with the rest of
audit/). Stdlib only; shells out to docker. See docs/runbooks/UPGRADES.md.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, re, subprocess, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "audit", "rollout-state.json")   # written only from the live checkout (guard)
DEFAULT_CANARY = "claude-runner-commons"
# (port, path) per service; everything else serves /health on 8080.
HEALTH = {"lanes": (8081, "/health"), "n8n": (5678, "/healthz")}
# Seconds a freshly (re)created service gets to answer 200 before verify calls it bad.
# n8n takes ~6-20s to boot (migrations, then "Editor is now accessible"); on 2026-09-18
# a single probe right after `up -d` got 0 and stopped a promote over a healthy n8n.
HEALTH_GRACE = {"n8n": 60}
DEFAULT_HEALTH_GRACE = 20
HEALTH_POLL_INTERVAL = 2.0


# ================================================================ pure decisions

def is_runner(svc):
    return bool(re.search(r"-runner(-|$)", svc))


def runner_base(svc):
    """claude-runner-commons -> claude-runner (the source directory holding tools/)."""
    m = re.match(r"^(.*?-runner)(?:-.*)?$", svc)
    return m.group(1) if m else None


def waves(services, canary=None):
    """Order services into rollout waves. Pure."""
    services = list(services)
    runners = sorted([s for s in services if is_runner(s)],
                     key=lambda s: (not s.startswith("claude-"), s))
    if canary is None:
        canary = DEFAULT_CANARY if DEFAULT_CANARY in services else (runners[0] if runners else None)
    if canary not in services:
        raise ValueError(f"canary {canary!r} is not a service in this stack")
    rest_runners = [s for s in runners if s != canary]
    tail = [s for s in ("router", "n8n") if s in services and s != canary]
    middle = sorted(s for s in services if s not in runners and s not in tail and s != canary)
    out = [("canary", [canary]), ("runners", rest_runners), ("support", middle)]
    out += [(s, [s]) for s in tail]
    return [(name, svcs) for name, svcs in out if svcs]


def same_checkout(root, working_dir):
    """Is this script's repo root the directory the live stack was started from?"""
    norm = lambda p: os.path.normcase(os.path.normpath(os.path.abspath(p or "")))
    return bool(working_dir) and norm(root) == norm(working_dir)


def health_detail(facts):
    d = f"got {facts.get('health')}"
    if facts.get("health_attempts") is not None:
        d += f" after {facts.get('health_waited') or 0:.1f}s, {facts['health_attempts']} attempt(s)"
    if facts.get("health_stopped"):
        d += f" ({facts['health_stopped']})"
    return d


def evaluate_service(facts):
    """facts about one service -> [(check, ok, detail)]. Pure.
    facts: {service, running, restarting, restart_count, health (int|None),
            pins_expected {pkg:ver}|None, pins_installed {pkg:ver}|None,
            probe (0|1|2|None), optional health_waited/health_attempts/health_stopped}"""
    s = facts["service"]
    checks = [("running", facts.get("running") and not facts.get("restarting"),
               f"running={facts.get('running')} restarting={facts.get('restarting')}"),
              ("no restarts since recreate", (facts.get("restart_count") or 0) == 0,
               f"RestartCount={facts.get('restart_count')}"),
              ("/health answers 200", facts.get("health") == 200, health_detail(facts))]
    exp = facts.get("pins_expected")
    if exp is not None:
        got = facts.get("pins_installed") or {}
        wrong = {k: (v, got.get(k)) for k, v in exp.items() if got.get(k) != v}
        checks.append(("agent CLI pins match the checkout", not wrong,
                       "; ".join(f"{k} pinned {v} installed {g}" for k, (v, g) in wrong.items()) or
                       ", ".join(f"{k}@{v}" for k, v in exp.items())))
    if facts.get("probe") is not None:
        checks.append(("tool enforcement probe", facts["probe"] == 0,
                       {0: "proven", 1: "BROKEN", 2: "inconclusive"}.get(facts["probe"], "?")))
    return checks


def wait_for_health(probe, dead, grace, interval=HEALTH_POLL_INTERVAL,
                    clock=time.monotonic, sleep=time.sleep):
    """Poll probe() until it answers 200, the container dies, or `grace` seconds pass.
    -> (last status, seconds waited, attempts, why it stopped short or None).

    probe() -> int|None; dead() -> a reason string when the container is no longer
    running cleanly (stopped, restarting, RestartCount > 0), else None — a crash-loop
    ends the wait at once instead of being given the whole window. Bounded: no sleep
    runs past the deadline, so the wait is at most grace + one probe's own timeout.
    clock and sleep are injectable so the bound is testable without waiting."""
    start, attempts = clock(), 0
    while True:
        attempts += 1
        status = probe()
        if status == 200:
            return status, clock() - start, attempts, None
        reason = dead()
        if reason:
            return status, clock() - start, attempts, reason
        left = grace - (clock() - start)
        if left <= 0:
            return status, clock() - start, attempts, f"no 200 within {grace}s"
        sleep(min(interval, left))


def attribute_errors(records, service, since, scope_to_host, provider_hosts):
    """Ledger records since `since` -> (decisions, reached_service, errors, samples). Pure.

    Success records do not name the runner that served them, so "traffic since the
    canary started" alone proves nothing about the canary. A decision REACHED a
    runner when it was dispatched to a model tier (not t0, not blocked before
    dispatch) and the runner map sends that scope/provider to this host: claude work
    goes to scopeToHost[scope], second-vendor work to providerHosts[provider]. A
    runner failure is logged as blocked "error:runner <host> returned ..." and names
    the host directly."""
    decisions, reached, errs, samples = 0, 0, 0, []
    host_re = re.compile(r"\brunner " + re.escape(service) + r"\b")
    for r in records:
        try:
            ts = dt.datetime.fromisoformat(str(r.get("ts", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts < since:
            continue
        decisions += 1
        b = str(r.get("blocked") or "")
        provider = r.get("provider")
        host = (scope_to_host or {}).get(r.get("scope")) if provider == "claude" else                (provider_hosts or {}).get(provider)
        dispatched = r.get("tier") not in (None, "t0") and (not b or b.startswith("error:runner"))
        if (dispatched and host == service) or host_re.search(b):
            reached += 1
        if b.startswith("error:") and host_re.search(b):
            errs += 1
            samples.append(b[:120])
    return decisions, reached, errs, samples


# ================================================================ docker

def sh(cmd, check=False, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}... failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def compose_files():
    import scope_source
    src = scope_source.resolve(ROOT)
    scoped = os.path.join(src["out_dir"], "compose.scopes.yml")
    files = [os.path.join(ROOT, "docker-compose.yml")]
    if os.path.isfile(scoped):
        files.append(scoped)
    return files


def compose(*args):
    cmd = ["docker", "compose"]
    for f in compose_files():
        cmd += ["-f", f]
    return cmd + list(args)


def project_name():
    """The Compose project, from `name:` in docker-compose.yml (pinned there on purpose)."""
    for line in open(os.path.join(ROOT, "docker-compose.yml"), encoding="utf-8"):
        m = re.match(r"^name:\s*(\S+)", line)
        if m:
            return m.group(1)
    raise RuntimeError("docker-compose.yml declares no project name")


def live_containers():
    """{service: container_id} for this project, read from the RUNNING containers' own
    compose labels. Deliberately not `docker compose ps`: that needs this checkout's
    .env to interpolate, and a checkout that is not the live one (a worktree) has none.
    Read-only inspection and the safety guard must work from anywhere — found when the
    guard, run from a worktree, crashed on RUNNER_TOKEN instead of refusing."""
    r = sh(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project_name()}",
            "--format", '{{.ID}}\t{{.Label "com.docker.compose.service"}}'])
    out = {}
    for line in r.stdout.splitlines():
        cid, _, svc = line.partition("\t")
        if svc:
            out[svc] = cid
    return out


def services(include_config=False):
    """Services to act on: those that exist, plus — only after the guard, from the live
    checkout whose .env interpolates — any compose declares that are not yet created."""
    svcs = set(live_containers())
    if include_config:
        r = sh(compose("config", "--services"), check=True, cwd=ROOT)
        svcs |= {x for x in r.stdout.split() if x}
    return sorted(svcs)


def container_of(svc):
    return live_containers().get(svc)


def inspect(cid):
    r = sh(["docker", "inspect", cid])
    return json.loads(r.stdout)[0] if r.returncode == 0 else None


def live_working_dir():
    for cid in live_containers().values():
        info = inspect(cid)
        wd = info and (info["Config"].get("Labels") or {}).get("com.docker.compose.project.working_dir")
        if wd:
            return wd
    return None


def health(cid, svc):
    port, path = HEALTH.get(svc, (8080, "/health"))
    url = f"http://127.0.0.1:{port}{path}"
    node = ("require('http').get(process.argv[1],r=>{console.log(r.statusCode);process.exit(0)})"
            ".on('error',()=>{console.log(0);process.exit(0)})")
    py = ("import sys,urllib.request\ntry: print(urllib.request.urlopen(sys.argv[1],timeout=5).status)\n"
          "except Exception: print(0)")
    for cmd in (["node", "-e", node, url], ["python3", "-c", py, url]):
        r = sh(["docker", "exec", cid] + cmd)
        if r.returncode == 0 and r.stdout.strip().isdigit():
            return int(r.stdout.strip())
    return None


def container_trouble(info):
    """Why a container is not running cleanly (ends a health wait early), or None."""
    if not info:
        return "container gone"
    st = info["State"]
    if not st.get("Running") or st.get("Restarting"):
        return f"not running (status {st.get('Status')})"
    if info.get("RestartCount"):
        return f"restarted {info['RestartCount']} time(s)"
    return None


def pins(cid, svc):
    """(expected from this checkout's <runner>/tools/package.json, installed in the container)."""
    base = runner_base(svc) if is_runner(svc) else None
    manifest = base and os.path.join(ROOT, base, "tools", "package.json")
    if not manifest or not os.path.isfile(manifest):
        return None, None
    expected = json.load(open(manifest, encoding="utf-8")).get("dependencies") or {}
    js = ("const d=require('/opt/agent-tools/package.json').dependencies,o={};for(const n in d){"
          "try{o[n]=require('/opt/agent-tools/node_modules/'+n+'/package.json').version}catch(e){o[n]=null}}"
          "console.log(JSON.stringify(o))")
    r = sh(["docker", "exec", cid, "node", "-e", js])
    try:
        installed = json.loads(r.stdout)
    except ValueError:
        installed = {}
    return expected, installed


def verify(svc, probe=False):
    cid = container_of(svc)
    if not cid:
        return [("container exists", False, "no container for this service")]
    code, waited, attempts, stopped = wait_for_health(
        lambda: health(cid, svc), lambda: container_trouble(inspect(cid)),
        HEALTH_GRACE.get(svc, DEFAULT_HEALTH_GRACE))
    # Judge running/restarts on the state AFTER the wait, so a crash during it counts.
    info = inspect(cid)
    if not info:
        return [("container exists", False, f"container vanished during the health wait ({stopped})")]
    st = info["State"]
    expected, installed = pins(cid, svc)
    facts = {"service": svc, "running": st.get("Running"), "restarting": st.get("Restarting"),
             "restart_count": info.get("RestartCount"), "health": code, "health_waited": waited,
             "health_attempts": attempts, "health_stopped": stopped,
             "pins_expected": expected, "pins_installed": installed, "probe": None}
    if probe and svc.startswith("claude-runner"):
        name = info["Name"].lstrip("/")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "probe_cli_enforcement.py"),
                            "--container", name])
        facts["probe"] = r.returncode
    return evaluate_service(facts)


def report(svc, checks):
    ok = all(c[1] for c in checks)
    print(f"  {'PASS' if ok else 'FAIL'}  {svc}")
    for name, good, detail in checks:
        print(f"          {'ok ' if good else 'BAD'} {name}: {detail}")
    return ok


# ================================================================ state + guards

def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(state, open(STATE, "w", encoding="utf-8"), indent=2)


def guard(args):
    wd = live_working_dir()
    if not same_checkout(ROOT, wd):
        print(f"REFUSED: this checkout is {ROOT}, but the running stack was started from {wd}.\n"
              "  Compose resolves ./audit and ./workspace against the compose file's directory, so\n"
              "  recreating from here would re-point production at THIS checkout's ledger and scope\n"
              "  tree. Run from the live checkout (and pull the change there first).")
        return False
    if not args.allow_dirty:
        dirty = sh(["git", "-C", ROOT, "status", "--porcelain", "--untracked-files=no"]).stdout.strip()
        if dirty:
            print("REFUSED: uncommitted changes in tracked files — a deploy should be a commit.\n"
                  "  Commit first, or pass --allow-dirty knowingly.\n" + dirty[:400])
            return False
    return True


def recreate(svc, args, state):
    before = container_of(svc)
    prev = inspect(before) if before else None
    steps = []
    if prev:
        steps.append(("remember", {"image_id": prev["Image"], "image_ref": prev["Config"]["Image"],
                                   "at": dt.datetime.now(dt.timezone.utc).isoformat()}))
    build = compose("build", svc)
    up = compose("up", "-d", "--no-deps", svc)
    if args.dry_run:
        print(f"  [dry-run] {' '.join(build)}\n  [dry-run] {' '.join(up)}")
        return True
    if prev:
        state.setdefault("previous", {})[svc] = steps[0][1]
        save_state(state)
    # `compose build` on an image-only service (n8n) is a no-op that exits 0, so any
    # non-zero exit from either step is a real failure and stops the rollout.
    for label, cmd in (("build", build), ("up", up)):
        r = sh(cmd, cwd=ROOT)
        if r.returncode != 0:
            print(f"  FAILED: compose {label} {svc}: {(r.stderr or r.stdout)[-400:]}")
            return False
    after = container_of(svc)
    moved = before != after
    print(f"  {svc}: {'recreated' if moved else 'unchanged (image and config identical) — left running'}")
    return True


# ================================================================ commands

def cmd_plan(args):
    svcs = services()
    wd = live_working_dir()
    print(f"Project: {project_name()}   live stack started from: {wd}")
    print(f"This checkout: {ROOT}  ->  "
          f"{'deploys allowed from here' if same_checkout(ROOT, wd) else 'READ-ONLY here (not the live checkout)'}")
    print()
    for name, group in waves(svcs, args.service):
        for s in group:
            cid = container_of(s)
            info = cid and inspect(cid)
            started = info["State"]["StartedAt"][:19] if info else "-"
            print(f"  {name:<8} {s:<26} started {started}")
    return 0


def cmd_verify(args):
    targets = args.services or services()
    ok = all([report(s, verify(s, probe=args.probe)) for s in targets])
    print("\nALL VERIFIED" if ok else "\nVERIFY FAILED")
    return 0 if ok else 1


def cmd_canary(args):
    if not guard(args):
        return 3
    svcs = services(include_config=True)
    canary = waves(svcs, args.service)[0][1][0]
    state = load_state()
    started = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    print(f"Canary: {canary}")
    if not recreate(canary, args, state):
        return 1
    if args.dry_run:
        return 0
    state["canary"] = {"service": canary, "started": started.isoformat()}
    save_state(state)
    ok = report(canary, verify(canary, probe=args.probe))
    print(f"\n{'CANARY HEALTHY' if ok else 'CANARY FAILED — run: python scripts/rollout.py rollback ' + canary}")
    if ok:
        print(f"Next: let traffic reach it, then\n  python scripts/rollout.py observe --since {started.isoformat()}")
    return 0 if ok else 1


def cmd_observe(args):
    state = load_state()
    svc = args.service or (state.get("canary") or {}).get("service")
    since_raw = args.since or (state.get("canary") or {}).get("started")
    if not svc or not since_raw:
        print("INCONCLUSIVE: no canary recorded — pass --service and --since")
        return 2
    since = dt.datetime.fromisoformat(since_raw.replace("Z", "+00:00"))
    # The LIVE stack's ledger — the router writes it under the checkout it was started
    # from (./audit is a bind mount). Reading this checkout's audit/ from anywhere else
    # would report "no traffic" over an empty file and call an unobserved canary idle.
    ledger = os.path.join(live_working_dir() or ROOT, "audit", "decisions.jsonl")
    if not os.path.isfile(ledger):
        print(f"INCONCLUSIVE: no ledger at {ledger}")
        return 2
    records = []
    if os.path.isfile(ledger):
        for line in open(ledger, encoding="utf-8"):
            try:
                records.append(json.loads(line.split(" ", 1)[1]))
            except (IndexError, ValueError):
                continue
    import scope_source
    try:
        rmap = json.load(open(os.path.join(scope_source.resolve(ROOT)["out_dir"], "context",
                                           "runner-map.json"), encoding="utf-8"))
    except (OSError, ValueError):
        rmap = {}
    total, reached, errs, samples = attribute_errors(records, svc, since, rmap.get("scopeToHost"),
                                                     rmap.get("providerHosts"))
    print(f"Ledger since {since.isoformat()}: {total} decision(s), {reached} dispatched to {svc}, "
          f"{errs} error(s) attributable to it")
    for s in samples[:5]:
        print(f"    {s}")
    if errs > args.max_errors:
        print(f"FAIL: more than {args.max_errors} canary error(s) — roll back: "
              f"python scripts/rollout.py rollback {svc}")
        return 1
    if reached == 0:
        print(f"INCONCLUSIVE: no work reached {svc} since the canary started — an idle canary "
              "proves nothing. Send real work through it, then observe again.")
        return 2
    print("CANARY CLEAN — promote: python scripts/rollout.py promote")
    return 0


def cmd_promote(args):
    if not guard(args):
        return 3
    state = load_state()
    svcs = services(include_config=True)
    canary = (state.get("canary") or {}).get("service") or args.service
    for name, group in waves(svcs, canary)[1:]:
        print(f"\nWave: {name}")
        for s in group:
            if not recreate(s, args, state):
                return 1
            if args.dry_run:
                continue
            if not report(s, verify(s, probe=args.probe and s.startswith("claude-runner"))):
                print(f"\nSTOPPED at {s}. Services after it were not touched. Roll back: "
                      f"python scripts/rollout.py rollback {s}")
                return 1
    print("\nPROMOTED" if not args.dry_run else "\n[dry-run] plan complete")
    return 0


def cmd_rollback(args):
    if not guard(args):
        return 3
    prev = (load_state().get("previous") or {}).get(args.service)
    if not prev:
        print(f"NO ROLLBACK POINT for {args.service}: this tool has not recreated it. "
              "Roll back by reverting the commit and running a canary.")
        return 2
    tag = ["docker", "tag", prev["image_id"], prev["image_ref"]]
    up = compose("up", "-d", "--no-deps", "--no-build", "--force-recreate", args.service)
    if args.dry_run:
        print(f"  [dry-run] {' '.join(tag)}\n  [dry-run] {' '.join(up)}")
        return 0
    if sh(tag).returncode != 0:
        print(f"FAILED: the previous image {prev['image_id'][:19]} is gone (pruned?). "
              "Revert the commit and redeploy instead.")
        return 1
    if sh(up, cwd=ROOT).returncode != 0:
        print(f"FAILED: compose up {args.service}")
        return 1
    ok = report(args.service, verify(args.service))
    print(f"\n{'ROLLED BACK' if ok else 'ROLLBACK FAILED VERIFY'} to image {prev['image_id'][:19]} "
          f"(from {prev['at'][:19]})")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("--service")
    p = sub.add_parser("verify"); p.add_argument("services", nargs="*"); p.add_argument("--probe", action="store_true")
    for name in ("canary", "promote"):
        p = sub.add_parser(name)
        p.add_argument("--service"); p.add_argument("--probe", action="store_true")
        p.add_argument("--dry-run", action="store_true"); p.add_argument("--allow-dirty", action="store_true")
    p = sub.add_parser("observe"); p.add_argument("--service"); p.add_argument("--since")
    p.add_argument("--max-errors", type=int, default=0)
    p = sub.add_parser("rollback"); p.add_argument("service")
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--allow-dirty", action="store_true")
    args = ap.parse_args(argv)
    try:
        return _dispatch(args)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 2


def _dispatch(args):
    return {"plan": cmd_plan, "verify": cmd_verify, "canary": cmd_canary, "observe": cmd_observe,
            "promote": cmd_promote, "rollback": cmd_rollback}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
