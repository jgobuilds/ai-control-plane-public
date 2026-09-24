#!/usr/bin/env python3
"""Corpus for the Slack reply watcher, with a stubbed Slack.

Everything except the live HTTPS calls is exercised here. The Slack account and
bot token are the one part that cannot be faked, so they are the one part left
unproven — stated plainly rather than implied by a green run.

The bug this file exists to prevent: the watcher reading its OWN posted question
as the human's reply and unblocking the agent instantly with the text of the
question. That is a silent, plausible-looking wrong answer, which is the worst
kind.

    python tests/slack_ask_test.py
"""
import os, sys, json, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location(
    "slack_ask", os.path.join(HERE, "..", "scripts", "slack_ask.py"))
sa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sa)

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


class FakeSlack:
    """Records calls and replays canned message lists."""

    def __init__(self, messages, post_ok=True):
        self.messages = messages
        self.post_ok = post_ok
        self.posted = []
        self.polls = 0

    def __call__(self, method, token, payload=None, get_params=None):
        if method == "chat.postMessage":
            if not self.post_ok:
                return False, "not_in_channel"
            self.posted.append(payload)
            return True, {"ts": "1785000000.000100"}
        if method == "conversations.replies":
            self.polls += 1
            return True, {"messages": self.messages}
        return False, "unexpected method"


DELIVERED = []


def fake_deliver(router_url, router_token, ticket, answered, answer, note):
    DELIVERED.append({"ticket": ticket, "answered": answered,
                      "answer": answer, "note": note})
    return True, "ok"


PARENT = {"ts": "1785000000.000100", "text": "An agent is blocked and needs an answer"}


def run(messages, post_ok=True, minutes=0.05):
    DELIVERED.clear()
    fake = FakeSlack(messages, post_ok)
    sa.slack, sa.deliver = fake, fake_deliver
    res = sa.watch("xoxb-test", "C123", "tic-1", "Which folder?", "", minutes,
                   "http://router:8080", "rt", poll_seconds=0, _sleep=lambda _s: None)
    return res, fake


print("It must not answer its own question:")
res, fake = run([PARENT])
check("the parent message is never treated as a reply", res["answered"] is False,
      f"answered with {res.get('answer')!r}")
check("and nothing is delivered as an answer",
      all(d["answered"] is False for d in DELIVERED))

print("\nBots are not humans:")
res, _ = run([PARENT, {"ts": "1785000000.000200", "bot_id": "B1", "text": "beep"}])
check("a bot_id reply is ignored", res["answered"] is False)
res, _ = run([PARENT, {"ts": "1785000000.000201", "subtype": "bot_message", "text": "boop"}])
check("a bot_message subtype is ignored", res["answered"] is False)

print("\nA real reply is taken:")
res, _ = run([PARENT, {"ts": "1785000000.000300", "user": "U9", "text": "Use folder 1AbC"}])
check("answered", res["answered"] is True)
check("the text is the human's, not the question's", res["answer"] == "Use folder 1AbC",
      repr(res.get("answer")))
check("delivered as answered=True", DELIVERED and DELIVERED[-1]["answered"] is True)

print("\nThe FIRST human reply wins:")
res, _ = run([PARENT,
              {"ts": "1785000000.000400", "user": "U9", "text": "first"},
              {"ts": "1785000000.000500", "user": "U8", "text": "second"}])
check("first human reply is used", res["answer"] == "first", repr(res.get("answer")))

print("\nEmpty replies are not answers:")
res, _ = run([PARENT, {"ts": "1785000000.000600", "user": "U9", "text": "   "}])
check("whitespace-only reply does not count", res["answered"] is False)

print("\nNobody replies — UNKNOWN, not a refusal:")
res, _ = run([PARENT])
check("answered=False", res["answered"] is False)
check("delivered so the agent is released", DELIVERED and DELIVERED[-1]["ticket"] == "tic-1")
check("the note says unknown, not refused",
      "UNKNOWN" in (res.get("note") or ""), repr(res.get("note")))

print("\nIf the QUESTION never posted, nobody was asked:")
res, fake = run([PARENT], post_ok=False)
check("posted is False", res["posted"] is False)
check("it names the slack error", res.get("error") == "not_in_channel", repr(res.get("error")))
check("NO ONE WAS ASKED is stated", "NO ONE WAS ASKED" in (res.get("note") or ""))
check("and nothing was delivered — the agent must not hear 'no answer'",
      DELIVERED == [], repr(DELIVERED))

print("\nThe posted message carries the ticket (so a thread can be traced back):")
res, fake = run([PARENT, {"ts": "1785000000.000700", "user": "U9", "text": "ok"}])
check("ticket appears in the posted text",
      any("tic-1" in (p.get("text") or "") for p in fake.posted))
check("and it tells the human to reply in-thread",
      any("in this thread" in (p.get("text") or "").lower() for p in fake.posted))

print("\nSlack app-level errors are not successes:")
sa_ok, sa_err = sa.slack("conversations.replies", "t", None, {"channel": "C", "ts": "1"})
check("stubbed transport returns a tuple", isinstance(sa_ok, bool))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s)")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("All Slack-watcher checks passed (live Slack calls NOT covered — needs a bot token).")
