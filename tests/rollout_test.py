#!/usr/bin/env python3
"""Prove the rollout tool's decisions — order, safety, verification, canary attribution — offline.

scripts/rollout.py recreates production containers, so its logic has to be right
before it runs, and none of it can be exercised in CI against a live stack. Every
decision it makes is a pure function; each is pinned here.

    waves              canary first, then remaining runners (claude before gemini),
                       then support services, then router, then n8n; an unknown
                       canary is refused
    same_checkout      a worktree or second clone is NOT the live checkout (the guard
                       that stops a deploy re-pointing ./audit and ./workspace)
    evaluate_service   not running, restarted, no /health, a CLI pin mismatch and a
                       broken or inconclusive probe each fail
    wait_for_health    a service that answers 200 after N attempts passes; one that
                       never does fails once the grace window closes; the wait is
                       bounded; a crash-loop ends the wait early and still fails
    attribute_errors   a runner error naming the canary counts; one naming another
                       runner does not; t0 and pre-dispatch blocks did not reach it;
                       second-vendor work maps through providerHosts; records before
                       the canary started are ignored

    python tests/rollout_test.py
"""
import datetime as dt, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import rollout as ro  # noqa: E402

fails = []


def expect(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


STACK = ["router", "scrubber", "lanes", "n8n", "gemini-runner-commons",
         "claude-runner-delivery", "claude-runner-commons"]


def main():
    print("Waves (callees before callers):")
    w = ro.waves(STACK)
    expect("default canary is claude-runner-commons", w[0] == ("canary", ["claude-runner-commons"]), w)
    expect("then the other runners, claude before gemini",
           w[1] == ("runners", ["claude-runner-delivery", "gemini-runner-commons"]), w)
    expect("then support services", w[2] == ("support", ["lanes", "scrubber"]), w)
    expect("router after every runner and support service", w[3] == ("router", ["router"]), w)
    expect("n8n last — it generates the traffic", w[-1] == ("n8n", ["n8n"]), w)
    flat = [s for _, g in w for s in g]
    expect("every service appears exactly once", sorted(flat) == sorted(STACK), flat)
    w2 = ro.waves(STACK, canary="gemini-runner-commons")
    expect("an explicit canary leads and is not repeated",
           w2[0] == ("canary", ["gemini-runner-commons"]) and
           w2[1] == ("runners", ["claude-runner-commons", "claude-runner-delivery"]), w2)
    try:
        ro.waves(STACK, canary="nope")
        expect("an unknown canary is refused", False)
    except ValueError:
        expect("an unknown canary is refused", True)
    expect("runner_base strips the scope suffix", ro.runner_base("claude-runner-commons") == "claude-runner"
           and ro.runner_base("gemini-runner") == "gemini-runner")

    print("\nThe deploy-from-the-live-checkout guard:")
    expect("the live checkout passes", ro.same_checkout(r"D:\code\ai-control-plane", r"D:\code\ai-control-plane"))
    expect("case and slash differences are the same directory on Windows",
           ro.same_checkout(r"d:/code/ai-control-plane/", r"D:\code\ai-control-plane") == (os.name == "nt"))
    expect("a worktree is refused", not ro.same_checkout(r"D:\code\acp-oprisk", r"D:\code\ai-control-plane"))
    expect("an unknown working dir is refused", not ro.same_checkout(r"D:\code\ai-control-plane", None))

    print("\nVerification:")
    good = {"service": "claude-runner-commons", "running": True, "restarting": False, "restart_count": 0,
            "health": 200, "pins_expected": {"@anthropic-ai/claude-code": "2.1.267"},
            "pins_installed": {"@anthropic-ai/claude-code": "2.1.267"}, "probe": 0}
    ok = lambda f: all(c[1] for c in ro.evaluate_service(f))
    expect("healthy runner with matching pins and a proven probe passes", ok(good))
    for field, bad, label in (("running", False, "not running"), ("restarting", True, "restarting"),
                              ("restart_count", 2, "restarted since recreate"), ("health", 503, "/health 503"),
                              ("health", None, "/health unreachable"),
                              ("pins_installed", {"@anthropic-ai/claude-code": "2.1.274"}, "CLI pin mismatch"),
                              ("probe", 1, "enforcement BROKEN"), ("probe", 2, "enforcement inconclusive")):
        expect(f"{label} fails", not ok({**good, field: bad}))
    expect("a service with no pins and no probe is judged on health alone",
           ok({"service": "router", "running": True, "restarting": False, "restart_count": 0,
               "health": 200, "pins_expected": None, "probe": None}))

    print("\nWaiting for /health after a recreate (fake clock — nothing sleeps):")

    class Clock:
        def __init__(self, probe_cost=0.0):
            self.t, self.slept, self.probe_cost = 0.0, 0.0, probe_cost

        def now(self):
            return self.t

        def sleep(self, s):
            assert s > 0, s
            self.t += s
            self.slept += s

    def run(answers, grace=60, dead=lambda: None, probe_cost=0.0):
        c, seq = Clock(probe_cost), iter(answers)

        def probe():
            c.t += c.probe_cost
            return next(seq, answers[-1])
        return ro.wait_for_health(probe, dead, grace, interval=2.0, clock=c.now, sleep=c.sleep), c

    (code, waited, n, why), _ = run([200])
    expect("an already-healthy service is probed once, without sleeping",
           (code, n, waited) == (200, 1, 0), (code, n, waited))
    (code, waited, n, why), _ = run([0, 0, 0, None, 0, 200])  # n8n still booting, then up
    expect("healthy after 6 attempts passes, and reports the wait",
           code == 200 and n == 6 and waited == 10 and why is None, (code, waited, n, why))
    booting = {**good, "service": "n8n", "pins_expected": None, "probe": None,
               "health": code, "health_waited": waited, "health_attempts": n}
    expect("  ... and evaluate_service passes it", ok(booting))
    (code, waited, n, why), c = run([0], grace=60)
    expect("never healthy fails after the window", code != 200 and why == "no 200 within 60s", (code, why))
    expect("  ... having waited the whole window and no more", waited == 60 and c.slept == 60, waited)
    expect("  ... with a bounded number of attempts (31)", n == 31, n)
    expect("  ... and evaluate_service fails it", not ok({**booting, "health": code}))
    (code, waited, n, why), _ = run([0], grace=60, probe_cost=5.0)  # every probe hits its 5s timeout
    expect("slow probes still bound the wait to grace + one probe timeout",
           waited <= 65 and n == 9, (waited, n))
    deaths = iter([None, None, "restarted 1 time(s)"])
    (code, waited, n, why), _ = run([0], dead=lambda: next(deaths))
    expect("a crash during the wait ends it at once instead of using the window",
           n == 3 and waited == 4 and why == "restarted 1 time(s)", (n, waited, why))
    expect("  ... and the service fails (RestartCount > 0)",
           not ok({**booting, "health": code, "restart_count": 1}))
    expect("a crash-looping service that answers 200 between restarts still fails on RestartCount",
           not ok({**booting, "health": 200, "restart_count": 3}))
    clean = {"State": {"Running": True, "Restarting": False, "Status": "running"}, "RestartCount": 0}
    expect("container_trouble: a clean container is fine", ro.container_trouble(clean) is None)
    expect("container_trouble: restarting, exited, restarted or gone ends the wait",
           all(ro.container_trouble(i) for i in (
               {**clean, "State": {"Running": True, "Restarting": True, "Status": "restarting"}},
               {**clean, "State": {"Running": False, "Restarting": False, "Status": "exited"}},
               {**clean, "RestartCount": 2}, None)))
    expect("n8n gets a longer boot grace than the runners",
           ro.HEALTH_GRACE["n8n"] > ro.HEALTH_GRACE.get("claude-runner-commons", ro.DEFAULT_HEALTH_GRACE))

    print("\nCanary attribution from the ledger:")
    since = dt.datetime(2026, 9, 17, 12, 0, tzinfo=dt.timezone.utc)
    scope_to_host = {"internal": "claude-runner-commons", "delivery": "claude-runner-delivery"}
    provider_hosts = {"gemini": "gemini-runner-commons"}
    recs = [
        {"ts": "2026-09-17T11:59:00Z", "scope": "internal", "provider": "claude", "tier": "t2"},  # before
        {"ts": "2026-09-17T12:01:00Z", "scope": "internal", "provider": "claude", "tier": "t2"},  # reached
        {"ts": "2026-09-17T12:02:00Z", "scope": "internal", "provider": None, "tier": "t0"},       # deterministic
        {"ts": "2026-09-17T12:03:00Z", "scope": "internal", "provider": "claude", "tier": "t3",
         "blocked": "approval-required"},                                                          # pre-dispatch
        {"ts": "2026-09-17T12:04:00Z", "scope": "delivery", "provider": "claude", "tier": "t2"},  # other runner
        {"ts": "2026-09-17T12:05:00Z", "scope": "internal", "provider": "claude", "tier": "t2",
         "blocked": "error:runner claude-runner-commons returned 500: boom"},                      # canary error
        {"ts": "2026-09-17T12:06:00Z", "scope": "delivery", "provider": "claude", "tier": "t2",
         "blocked": "error:runner claude-runner-delivery returned 502: x"},                        # not the canary
    ]
    total, reached, errs, samples = ro.attribute_errors(recs, "claude-runner-commons", since,
                                                        scope_to_host, provider_hosts)
    expect("records before the canary started are ignored", total == 6, total)
    expect("only dispatched work mapped to the canary counts as reaching it (2)", reached == 2, reached)
    expect("only errors naming the canary are attributed to it (1)", errs == 1 and "commons" in samples[0],
           (errs, samples))
    g = ro.attribute_errors([{"ts": "2026-09-17T12:01:00Z", "scope": "internal", "provider": "gemini",
                              "tier": "t1"}], "gemini-runner-commons", since, scope_to_host, provider_hosts)
    expect("second-vendor work maps through providerHosts", g[1] == 1, g)
    idle = ro.attribute_errors(recs[2:4], "claude-runner-commons", since, scope_to_host, provider_hosts)
    expect("traffic that never reached the canary leaves it at 0 reached (inconclusive, not clean)",
           idle[1] == 0 and idle[0] == 2, idle)

    print()
    if fails:
        print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
        return 1
    print("All rollout checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
