#!/usr/bin/env python3
"""Post a CI diagnosis where the failure is already being discussed.

An alert that says "CI failed, here is a link" makes the reader go find the
cause. This puts the cause next to the failure: a comment on the PR (or the
commit), and a threaded reply under the Slack alert.

WHY THIS IS A SEPARATE FILE. scripts/ci_diagnose.py has NO write path at all —
no network, no subprocess — and a test asserts that structurally, because the
cheapest way to make CI green is to weaken a check and a diagnostician that can
push learns to do exactly that. Posting a comment obviously needs the network.
Keeping the two apart is what lets that guarantee stay absolute instead of
becoming "no write path except the parts that do". This file writes COMMENTS.
It cannot commit, push, edit a file, or rerun a job, and it takes the diagnosis
as input rather than deciding anything itself.

WHERE IT POSTS

  GitHub — zero configuration; the built-in GITHUB_TOKEN is enough.
    pull_request  -> a comment on the PR
    push          -> a comment on the commit
    always        -> the job summary on the run page

  Slack — only with a bot token. Incoming webhooks return no message `ts`, so a
    webhook alert CANNOT be threaded; that is a property of webhooks, not a
    missing feature here. With SLACK_BOT_TOKEN + SLACK_CHANNEL_ID it finds the
    alert for this run and replies in its thread.

The alert itself carries one BLUF line (`ci_diagnose.py --bluf`). Detail lives
here, which is the notification standard's shape: interrupt stays short, detail
is one click away.

    python scripts/ci_comment.py --diagnosis diag.json --target github
    python scripts/ci_comment.py --diagnosis diag.json --target slack
    python scripts/ci_comment.py --diagnosis diag.json --target both --dry-run

Always exits 0. It reports on a failure that already happened; failing here
would hide the diagnosis behind a second red X.
"""
from __future__ import annotations
import os, sys, json, argparse, urllib.request, urllib.error, urllib.parse

# Every posted comment carries this. A re-run of the same job must UPDATE its
# comment rather than add another — a flaky job re-run five times would
# otherwise bury the PR in five identical diagnoses, and the noise would get the
# whole mechanism turned off.
MARKER = "<!-- ci-diagnosis:do-not-remove -->"
# Where ci_diagnose's prompt stops quoting the log and starts asking
# questions. A named constant rather than a prose match, so rewording the
# prompt cannot silently empty the excerpt in every comment.
ANSWER_MARKER = "\n\nAnswer as a ROOT-CAUSE REVIEW"
SLACK_API = "https://slack.com/api/"


# ------------------------------------------------------------------ rendering

def body_markdown(res, run_url=""):
    """The comment. BLUF heading, then the supporting detail underneath."""
    L = [MARKER, ""]
    # The single most load-bearing fact about a red build, and the run list does
    # not show it. A first-run failure and a regression look identical there and
    # need opposite responses — one repo here failed 7 for 7 while everyone
    # looked for what the last commit broke.
    if res.get("ever_green") is False:
        L += ["> [!IMPORTANT]",
              "> **This workflow has never passed — not once in its recorded "
              "history.** Suspect the GATE before the commit. A gate that has "
              "never been green may be unsatisfiable, may be diffing a file the "
              "build regenerates, or may have been enabled before the thing it "
              "checks existed. Nothing you change in this commit is likely to "
              "fix it.", ""]
    if res["tier"] == "deterministic":
        n = len(res["known"])
        L.append(f"### 🔎 CI diagnosis — {n} known cause{'s' if n != 1 else ''}")
        L.append("")
        L.append("This failure has been seen and fixed before, so the cause below "
                 "is a lookup, not a guess. No model was called.")
        for k in res["known"]:
            L += ["", f"**{k['id']}**  ·  confidence: {k.get('confidence', 'unknown')}",
                  "", f"- **Cause:** {k['cause']}", f"- **Fix:** {k['fix']}"]
            # Fix and prevention are different work, and the second is the one
            # that gets skipped once the build is green again.
            if k.get("prevent"):
                L.append(f"- **Prevent:** {k['prevent']}")
            else:
                L.append("- **Prevent:** _no mechanism recorded for this cause "
                         "yet. If you find one, add a `prevent` field to its row "
                         "— that is what stops the third occurrence._")
    elif res["tier"] == "escalate":
        L += ["### ❓ CI diagnosis — unrecognised failure", "",
              "No known signature matched, so **no cause is being claimed**. "
              "A confident wrong cause costs more than no cause.", "",
              "Once this is diagnosed, record it as a root-cause review, not "
              "just a fix: what broke, why that was possible, **why nothing "
              "surfaced it sooner**, and the MECHANISM that prevents recurrence. "
              "Then add a row to `.ci-known-causes.json` with both `fix` and "
              "`prevent` — the next occurrence is free, and the one after that "
              "does not happen. See `docs/runbooks/CI-DIAGNOSIS.md`."]
        excerpt = res.get("escalation", {}).get("prompt", "")
        # The prompt embeds the redacted failing lines; show them rather than
        # making the reader open the log. Redaction happened in ci_diagnose.
        if "FAILING LINES" in excerpt:
            lines = excerpt.split("«markers» — do not speculate about their contents):\n", 1)
            if len(lines) == 2:
                # Split on a named marker, not on the prompt's prose. The first
                # version keyed on "Answer three things" and silently stopped
                # extracting the moment that prompt was reworded into a
                # root-cause review — the excerpt would have disappeared from
                # every comment with nothing failing to say so.
                snippet = lines[1].split(ANSWER_MARKER)[0].strip()
                L += ["", "<details><summary>Failing lines (secrets redacted)</summary>",
                      "", "```", snippet[:4000], "```", "", "</details>"]
    elif res["tier"] == "no-log":
        L += ["### 🚫 CI diagnosis — the failure log could not be read", "",
              "**Nothing was diagnosed.** This is not the same as an "
              "unrecognised failure, and it deliberately does not render like "
              "one: a fetch that fails looks exactly like a short log with no "
              "known signature, so without this the comment would confidently "
              "report every failure as unrecognised.", "", res.get("note", "")]
        if res.get("detail"):
            L += ["", "```", res["detail"][:1000], "```"]
    else:
        L += ["### ⚠️ CI diagnosis unavailable", "", res.get("note", "")]
    L += ["", "---",
          "_Advise only — this tool cannot change code, and deliberately so: the "
          "cheapest way to make CI green is usually to weaken a check._"]
    if run_url:
        L.append(f"  [View run]({run_url})")
    return "\n".join(L)


# ------------------------------------------------------------------- transport

def http(url, token, payload=None, method=None, scheme="Bearer"):
    """One API call. Returns (ok, data_or_error).

    Slack answers HTTP 200 with {"ok": false, "error": "..."} for application
    errors, so status alone tells you almost nothing — a caller that checks only
    the status reads "not_in_channel" as success.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"{scheme} {token}",
               "Accept": "application/vnd.github+json"}
    if data:
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
            body = json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.read()[:200].decode('utf-8', 'replace')}"
    except Exception as e:                                   # noqa: BLE001
        return False, type(e).__name__
    if isinstance(body, dict) and body.get("ok") is False:
        return False, body.get("error") or "unknown error"
    return True, body


# ---------------------------------------------------------------------- github

def github_target(env):
    """(list_url, create_url) for the thing to comment on, or (None, None).

    A PR comment goes on the ISSUE endpoint, not the pulls endpoint: the pulls
    one creates a review comment, which must be anchored to a diff line and 422s
    without one. Easy to get wrong; the reason is recorded so it stays right.
    """
    repo = env.get("GITHUB_REPOSITORY", "")
    api = env.get("GITHUB_API_URL", "https://api.github.com")
    if not repo:
        return None, None
    ref = env.get("GITHUB_REF", "")            # refs/pull/123/merge
    if env.get("GITHUB_EVENT_NAME") == "pull_request" and "/pull/" in ref:
        num = ref.split("/pull/")[1].split("/")[0]
        u = f"{api}/repos/{repo}/issues/{num}/comments"
        return u, u
    sha = env.get("GITHUB_SHA", "")
    if sha:
        u = f"{api}/repos/{repo}/commits/{sha}/comments"
        return u, u
    return None, None


def post_github(body, env, dry_run=False):
    token = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN") or ""
    list_url, create_url = github_target(env)
    if not list_url:
        return False, "no PR or commit to comment on (not running in Actions?)"
    if not token:
        return False, "no GITHUB_TOKEN — the workflow needs pull-requests: write"
    if dry_run:
        return True, f"[dry-run] would post to {create_url}"

    # Update in place if we already commented on this PR. Commit comments have
    # no reliable list-then-update path across event types, so a repeat there
    # appends; PRs are where re-runs actually pile up.
    ok, existing = http(list_url + "?per_page=100", token)
    if ok and isinstance(existing, list):
        for c in existing:
            if MARKER in (c.get("body") or ""):
                ok2, res = http(c["url"], token, {"body": body}, method="PATCH")
                return ok2, ("updated existing comment" if ok2 else res)
    ok, res = http(create_url, token, {"body": body})
    return ok, (res.get("html_url", "posted") if ok else res)


# ----------------------------------------------------------------------- slack

def find_alert_ts(token, channel, run_url):
    """The alert message for THIS run, so the reply lands in its thread.

    Matched on the run URL, which is unique per run, rather than on recency —
    two failures a minute apart would otherwise thread onto each other's alerts.
    """
    ok, body = http(SLACK_API + "conversations.history?"
                    + urllib.parse.urlencode({"channel": channel, "limit": "30"}),
                    token)
    if not ok:
        return None, body
    for m in body.get("messages", []):
        if run_url and run_url in json.dumps(m):
            return m.get("ts"), None
    return None, "no alert message for this run found in the last 30 messages"


def post_slack(body, env, run_url, dry_run=False):
    token = env.get("SLACK_BOT_TOKEN", "")
    channel = env.get("SLACK_CHANNEL_ID", "")
    if not (token and channel):
        # Not an error. A webhook-only setup is the normal, supported case; it
        # simply cannot thread, because the webhook never returns a message ts.
        return False, ("no SLACK_BOT_TOKEN/SLACK_CHANNEL_ID — skipping the thread "
                       "reply. A webhook alert cannot be threaded; that is a "
                       "webhook limitation, not a missing feature here")
    if dry_run:
        return True, "[dry-run] would reply in thread"
    ts, err = find_alert_ts(token, channel, run_url)
    if not ts:
        return False, f"could not thread: {err}"
    ok, res = http(SLACK_API + "chat.postMessage", token,
                   {"channel": channel, "thread_ts": ts,
                    "text": slackify(body)})
    return ok, ("replied in thread" if ok else res)


def slackify(md):
    """Markdown -> Slack mrkdwn, for the parts that actually differ.

    Slack is not markdown: **bold** is *bold*, and headings/HTML do not render.
    Left deliberately small — a full converter here would be a second thing to
    maintain for one message shape.
    """
    import re
    # Slack renders [text](url) as literal characters. Absolute links become
    # Slack's <url|text>; a repo-relative one has no meaning outside GitHub, so
    # only its text survives.
    def _link(m):
        text, href = m.group(1), m.group(2)
        return f"<{href}|{text}>" if "://" in href else text

    out = []
    for line in md.splitlines():
        if line.startswith(MARKER) or line.startswith("<details") or \
           line.startswith("</details") or line.startswith("---"):
            continue
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, line)
        line = line.replace("### ", "*").replace("**", "*")
        if line.startswith("*") and not line.startswith("* "):
            line = line.rstrip() + ("*" if line.count("*") % 2 else "")
        out.append(line)
    return "\n".join(out).strip()[:3500]


# ------------------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Post a CI diagnosis (comments only).")
    ap.add_argument("--diagnosis", required=True,
                    help="JSON from `ci_diagnose.py --json`; '-' for stdin")
    ap.add_argument("--target", default="both", choices=["github", "slack", "both"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    raw = sys.stdin.read() if a.diagnosis == "-" else open(
        a.diagnosis, encoding="utf-8").read()
    try:
        res = json.loads(raw)
    except ValueError as e:
        print(f"ci_comment: diagnosis is not valid JSON ({e}) — nothing posted")
        return 0

    env = os.environ
    run_url = ""
    if env.get("GITHUB_SERVER_URL") and env.get("GITHUB_RUN_ID"):
        run_url = (f"{env['GITHUB_SERVER_URL']}/{env.get('GITHUB_REPOSITORY','')}"
                   f"/actions/runs/{env['GITHUB_RUN_ID']}")
    body = body_markdown(res, run_url)

    # The run page always gets it, with no token and no configuration at all.
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary and not a.dry_run:
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(body + "\n")
            print("  job summary: written")
        except OSError as e:
            print(f"  job summary: {type(e).__name__}")

    for name, fn in (("github", lambda: post_github(body, env, a.dry_run)),
                     ("slack", lambda: post_slack(body, env, run_url, a.dry_run))):
        if a.target in (name, "both"):
            ok, detail = fn()
            print(f"  {name}: {'ok' if ok else 'skipped'} — {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
