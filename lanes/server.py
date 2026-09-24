#!/usr/bin/env python3
"""lanes — a tiny HTTP front for the scheduled PYTHON lanes, so n8n can drive them.

WHY THIS EXISTS. n8n's image is a Docker Hardened Image with no package manager and
no python, and no other service in the stack has python either (see
docs/decisions/0006-python-lanes-run-host-side.md). But orchestration, scheduling,
execution logs and notification should stay in n8n — the common infra. So instead of
n8n shelling out to python (impossible), n8n makes an HTTP call to this service,
which runs the same scripts/*.py and returns their JSON payload.

n8n keeps: the schedule, the execution record, the IF gate, the Notify seam.
This keeps: python.

EDITING THIS FILE REQUIRES A REBUILD. `./scripts` is bind-mounted, so changes to
scripts/*.py take effect immediately — but THIS file is baked into the image at
/srv/server.py. A new endpoint added here 404s ("no such lane") until
`docker compose up -d --build lanes`, and the surprise is precisely that
everything beside it behaves the other way. Two unattended runs errored on this.

Endpoints (all GET, all JSON):
  /health                -> {"ok": true}
  /digest?days=1         -> scripts/daily_digest.py payload
                            {post, severity, title, message, source, meta}
  /eval-metrics?days=7   -> scripts/eval_metrics.py analyze() result
  /recurrence?days=90    -> scripts/recurrence.py build() — what has failed more
                            than once, what was done about it, and which
                            dispositions are about to expire. READ-ONLY: it never
                            collects, so it cannot write to the ro audit mount
  /dispatch?assignee=X   -> scripts/dispatch.py — claims ONE ticket and returns
                            the handoff for the router. Single writer; see
                            docs/decisions/0012-work-dispatch.md
  /research?days=7       -> scripts/research_watch.py report() — releases in the
                            things we depend on that could move a RECORDED decision
  /slack-ask?ticket=&question=[&context=][&minutes=]
                         -> start a Slack thread watch for an ask-human question.
                            Returns IMMEDIATELY; the answer is delivered to the
                            router, not to the caller.

AUTH: fail-closed like the rest of the stack. Requires header `x-lanes-token` to
match LANES_TOKEN. If LANES_TOKEN is unset the service refuses to start, unless
ALLOW_NO_AUTH=1 (local dev only — same escape hatch the other services use).
Bound to the internal docker network only; no host port is published.

Pure stdlib (http.server) — consistent with the rest of ai-control-plane' python.
"""
import os, sys, json, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SCRIPTS = os.environ.get("LANES_SCRIPTS", "/app/scripts")
APP_ROOT = os.environ.get("LANES_APP_ROOT", "/app")
LEDGER = os.environ.get("LANES_LEDGER", "/audit/decisions.jsonl")
TOKEN = os.environ.get("LANES_TOKEN", "")
ALLOW_NO_AUTH = os.environ.get("ALLOW_NO_AUTH") == "1"
PORT = int(os.environ.get("LANES_PORT", "8081"))

sys.path.insert(0, SCRIPTS)  # so `import daily_digest` / `eval_metrics` resolve


def _int(qs, key, default):
    try:
        return int(qs.get(key, [default])[0])
    except (TypeError, ValueError):
        return default


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # quiet; n8n holds the execution record
        pass

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)

        if u.path == "/health":
            return self._send(200, {"ok": True})

        # Fail closed on every non-health route.
        if not ALLOW_NO_AUTH and self.headers.get("x-lanes-token", "") != TOKEN:
            return self._send(401, {"error": "unauthorized"})

        try:
            if u.path == "/digest":
                import daily_digest
                payload = daily_digest.build(
                    LEDGER, _int(qs, "days", 1), None, _int(qs, "heartbeatWeekday", 0))
                return self._send(200, payload)

            if u.path == "/eval-metrics":
                import eval_metrics, datetime
                end = datetime.datetime.now(datetime.timezone.utc)
                start = end - datetime.timedelta(days=_int(qs, "days", 7))
                return self._send(200, eval_metrics.analyze(LEDGER, start, end, None))

            if u.path == "/retention":
                # DRY-RUN ONLY, BY CONSTRUCTION. `--apply` is never passed and there
                # is no parameter that could add it, so this lane cannot delete a
                # file or prune the vault no matter what calls it. Enforcing that
                # here (capability) rather than in the workflow (configuration)
                # means a mis-edited workflow still cannot destroy anything.
                import subprocess as sp
                r = sp.run([sys.executable, os.path.join(SCRIPTS, "retention_sweep.py"),
                            "--root", APP_ROOT],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
                vault_seen = os.path.isdir(os.path.join(APP_ROOT, "vault"))
                return self._send(200, {
                    "mode": "dry-run",
                    "applied": False,
                    "vault_inspected": vault_seen,
                    # Say it plainly: an un-inspected vault must never be mistaken
                    # for a vault with nothing to prune.
                    "note": ("token-vault pruning WAS inspected" if vault_seen else
                             "token-vault is NOT mounted into this service, so vault "
                             "pruning was NOT inspected — file retention only"),
                    "exit_code": r.returncode,
                    "report": (r.stdout or "").strip(),
                    "stderr": (r.stderr or "").strip()[-500:],
                })

            if u.path == "/recurrence":
                # READ-ONLY BY CONSTRUCTION, like /retention is dry-run by
                # construction. `collect` writes; this never calls it, and there is
                # no parameter that could. That is not a limitation to work around
                # — /audit is mounted ro here on purpose, and the register itself
                # is a tracked file that a scheduled lane has no business editing.
                #
                # build(live=True) re-derives the ROUTER source from the mounted
                # ledger in memory, so the weekly message reports current router
                # faults with no write access at all. The CI source has no durable
                # store on this host, so it is only as fresh as the last host-side
                # collect — and the payload's `sources` says how stale, because
                # "no CI failures" and "nobody has collected since April" produce
                # the identical empty list.
                import recurrence
                return self._send(200, recurrence.build(
                    days=_int(qs, "days", 90), audit=LEDGER))

            if u.path == "/research":
                # FETCH-SIDE ONLY. This service has egress; the agent runners are
                # firewalled default-deny (verified: github.com times out from
                # claude-runner). So the network hop happens here and the REASONING
                # happens in the runner, over the JSON this returns. The runner
                # never reaches the internet — same split as drive-sync.
                import research_watch
                return self._send(200, research_watch.report(_int(qs, "days", 7)))

            if u.path == "/dispatch":
                # ONE DISPATCHER, ONE CLAIM (ADR 0012). This endpoint is the
                # single writer of ticket ownership, which is the entire reason
                # there is no compare-and-set anywhere: a single writer cannot
                # race itself. Running a SECOND dispatcher against the same
                # ticket store breaks that guarantee silently — two claims, two
                # agents, one ticket — so if throughput ever needs a second one,
                # build the lease (ADR 0010 N2) first.
                #
                # It assigns and returns a handoff. It does NOT execute: n8n
                # takes this payload to the ROUTER, which is the only thing that
                # reaches a runner. Dispatching straight to a runner from here
                # would put a second authority beside the router — the open C2
                # finding in the threat model.
                import dispatch as _dispatch
                import ticket as _ticket
                backend = _ticket.get_backend(
                    qs.get("backend", [None])[0], qs.get("repo", [None])[0])
                ok, why = backend.available()
                if not ok:
                    # Refuse rather than report an empty queue. "No work" and
                    # "cannot see the work" must never look the same — one is a
                    # healthy fleet, the other is a broken one.
                    return self._send(503, {"dispatched": None,
                                            "error": f"ticket backend {backend.name!r} "
                                                     f"unavailable: {why}"})
                return self._send(200, _dispatch.dispatch(
                    backend,
                    label=qs.get("label", ["agent-ready"])[0],
                    dry_run=qs.get("dry", ["0"])[0] not in ("0", "", "false"),
                    assignee=qs.get("assignee", [None])[0]))

            if u.path == "/slack-ask":
                # ASYNC BY CONSTRUCTION. The caller (the ask-human flow) must not
                # wait: it has its own form to put in front of a human, and n8n
                # runs branches in sequence — a blocking call here would mean the
                # Slack watch never starts until the form was already answered.
                # So spawn and return.
                #
                # Returns 200 EVEN WHEN UNCONFIGURED. Slack is an enhancement on
                # top of the form; if it is missing or broken the primary path
                # must still work. An error here would fail the workflow and take
                # the form down with it.
                import slack_ask, threading
                ticket = (qs.get("ticket") or [""])[0]
                question = (qs.get("question") or [""])[0]
                if not ticket or not question:
                    return self._send(200, {"started": False,
                                            "reason": "ticket and question are required"})
                bot = os.environ.get("SLACK_BOT_TOKEN", "")
                chan = os.environ.get("SLACK_CHANNEL_ID", "")
                rtok = os.environ.get("ROUTER_TOKEN", "")
                missing = [n for n, v in (("SLACK_BOT_TOKEN", bot),
                                          ("SLACK_CHANNEL_ID", chan),
                                          ("ROUTER_TOKEN", rtok)) if not v]
                if missing:
                    return self._send(200, {"started": False, "missing": missing,
                                            "reason": "slack answering not configured — "
                                                      "the form remains the answer path"})
                t = threading.Thread(
                    target=slack_ask.watch, daemon=True,
                    args=(bot, chan, ticket, question,
                          (qs.get("context") or [""])[0],
                          _int(qs, "minutes", 10),
                          os.environ.get("ROUTER_URL", "http://router:8080"), rtok))
                t.start()
                return self._send(200, {"started": True, "ticket": ticket})

            return self._send(404, {"error": "no such lane", "path": u.path})
        except Exception as e:  # never go dark — surface the failure as data
            return self._send(500, {"error": f"{type(e).__name__}: {e}",
                                    "trace": traceback.format_exc()[-800:]})


if __name__ == "__main__":
    if not TOKEN and not ALLOW_NO_AUTH:
        print("LANES_TOKEN is unset — refusing to start (set it, or ALLOW_NO_AUTH=1 "
              "for local dev only).", file=sys.stderr)
        sys.exit(2)
    print(f"lanes listening on :{PORT}  scripts={SCRIPTS}  ledger={LEDGER}", flush=True)
    # THREADING, not the single-threaded default. One long request used to block
    # every other lane behind it: a ten-minute Slack watch would have frozen the
    # digest, eval, retention and research lanes with it. The lanes are
    # independent — an analysis over the ledger, a dry-run sweep, a network fetch
    # — so serialising them bought nothing and cost availability.
    #
    # daemon_threads so a watch in progress cannot hold the container open on
    # shutdown; a container that will not stop is worse than a dropped watch.
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()
