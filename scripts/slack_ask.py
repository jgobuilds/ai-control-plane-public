#!/usr/bin/env python3
"""Ask a question in Slack and watch the thread for a human reply.

WHY POLLING, NOT EVENTS. Slack can only push to a URL it can reach, and this
stack is loopback-only by design (ADR 0009). Socket Mode would avoid that with an
outbound WebSocket, but it needs a WebSocket client, and `lanes` is stdlib-only
on purpose. Polling `conversations.replies` needs nothing but outbound HTTPS,
which this service already does for the research watch.

The cost is honest and small: a reply is seen within one poll interval rather
than instantly, and each poll is one API call against Slack's per-method limits.

WHY IT DOES NOT REPLACE THE FORM. The form path already works. This delivers to
the SAME router endpoint, and the runner's mailbox is read-once — so whichever
answer arrives first wins and the other is discarded. Two channels, one answer,
no coordination needed.

    python scripts/slack_ask.py --ticket <uuid> --question "..." --minutes 10

Requires (fail-closed, see main):
    SLACK_BOT_TOKEN    xoxb-… with chat:write and channels:history (or
                       groups:history for a private channel)
    SLACK_CHANNEL_ID   the channel to ask in; the bot must be a member
    ROUTER_URL         default http://router:8080
    ROUTER_TOKEN       same token n8n uses to call the router
"""
from __future__ import annotations
import os, sys, json, time, argparse, urllib.request, urllib.error, urllib.parse

SLACK_API = "https://slack.com/api/"


def slack(method, token, payload=None, get_params=None):
    """One Slack API call. Returns (ok, data_or_error_string).

    Slack answers 200 with {"ok": false, "error": "..."} for application errors,
    so the HTTP status alone tells you almost nothing — a caller that checks only
    the status treats "not_in_channel" as success.
    """
    url = SLACK_API + method
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if get_params:
        url += "?" + urllib.parse.urlencode(get_params)
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:                       # noqa: BLE001
        return False, type(e).__name__
    if not body.get("ok"):
        return False, body.get("error") or "unknown slack error"
    return True, body


def post_question(token, channel, ticket, question, context=""):
    """Post the question and return its thread timestamp.

    chat.postMessage rather than the incoming webhook, because the webhook does
    not return a `ts` — and without the ts there is no thread to watch.
    """
    lines = [f":raising_hand: *An agent is blocked and needs an answer*",
             "", question]
    if context:
        lines += ["", f"_{context}_"]
    lines += ["", "Reply *in this thread* to answer.",
              f"`ticket {ticket}`"]
    ok, body = slack("chat.postMessage", token, {
        "channel": channel, "text": "\n".join(lines),
        "unfurl_links": False, "unfurl_media": False,
    })
    if not ok:
        return None, body
    return body.get("ts"), None


def first_human_reply(token, channel, thread_ts):
    """The first reply in the thread that a PERSON wrote.

    Two exclusions, and both are load-bearing:
      - the parent message itself (ts == thread_ts), which is our own question;
      - anything with a bot_id or matching our own bot, or the watcher answers
        its own question the instant it is asked.
    """
    ok, body = slack("conversations.replies", token, None,
                     {"channel": channel, "ts": thread_ts, "limit": 50})
    if not ok:
        return None, body
    for m in body.get("messages") or []:
        if m.get("ts") == thread_ts:
            continue                       # our own question
        if m.get("bot_id") or m.get("subtype") == "bot_message":
            continue                       # never treat a bot as the human
        text = (m.get("text") or "").strip()
        if text:
            return text, None
    return None, None                      # nobody has replied yet


def deliver(router_url, router_token, ticket, answered, answer, note):
    """Hand the answer to the router, which forwards it to the blocked runner."""
    data = json.dumps({
        "ticket": ticket, "provider": "claude",
        "answered": answered, "answer": answer,
        "timedOut": not answered, "note": note,
    }).encode()
    req = urllib.request.Request(
        router_url.rstrip("/") + "/ask/answer", data=data,
        headers={"Content-Type": "application/json", "x-router-token": router_token})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200, r.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:                       # noqa: BLE001
        return False, type(e).__name__


def watch(token, channel, ticket, question, context, minutes,
          router_url, router_token, poll_seconds=5, _sleep=time.sleep):
    """Post, watch, deliver. Returns a result dict describing what happened."""
    ts, err = post_question(token, channel, ticket, question, context)
    if not ts:
        # Never silently swallow this: if the question was never asked, the agent
        # must not be told "nobody answered".
        return {"posted": False, "error": err, "answered": False,
                "note": f"slack post failed ({err}) — NO ONE WAS ASKED"}

    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        _sleep(poll_seconds)
        reply, err = first_human_reply(token, channel, ts)
        if err:
            continue                       # transient; keep watching
        if reply:
            ok, detail = deliver(router_url, router_token, ticket, True, reply,
                                 "answered in Slack thread")
            return {"posted": True, "thread_ts": ts, "answered": True,
                    "answer": reply, "delivered": ok, "detail": detail}

    ok, detail = deliver(router_url, router_token, ticket, False, "",
                         "no Slack reply before the timeout")
    return {"posted": True, "thread_ts": ts, "answered": False,
            "delivered": ok, "detail": detail,
            "note": "nobody replied in the thread — UNKNOWN, not a refusal"}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Ask in Slack, watch for a reply.")
    ap.add_argument("--ticket", required=True)
    ap.add_argument("--question", required=True)
    ap.add_argument("--context", default="")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--poll-seconds", type=float, default=5.0)
    a = ap.parse_args(argv)

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    channel = os.environ.get("SLACK_CHANNEL_ID", "")
    router_url = os.environ.get("ROUTER_URL", "http://router:8080")
    router_token = os.environ.get("ROUTER_TOKEN", "")

    # FAIL CLOSED, and name the missing thing. "It didn't work" costs more than
    # any of these messages.
    missing = [n for n, v in (("SLACK_BOT_TOKEN", token),
                              ("SLACK_CHANNEL_ID", channel),
                              ("ROUTER_TOKEN", router_token)) if not v]
    if missing:
        print(json.dumps({"error": "not configured", "missing": missing,
                          "hint": "a bot token is required; the incoming webhook "
                                  "used for notifications is send-only and cannot "
                                  "read replies"}), file=sys.stderr)
        return 2

    res = watch(token, channel, a.ticket, a.question, a.context, a.minutes,
                router_url, router_token, a.poll_seconds)
    print(json.dumps(res, indent=2))
    return 0 if res.get("answered") else 1


if __name__ == "__main__":
    raise SystemExit(main())
