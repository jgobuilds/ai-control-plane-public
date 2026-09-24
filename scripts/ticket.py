#!/usr/bin/env python3
"""File and update tickets from an agent — GitHub Issues by default, swappable.

WHY A LAYER AT ALL, given the rule is to adopt rather than build. Because the
choice of tracker is exactly the kind of decision that gets made once and then
becomes impossible to revisit: every lane, hook and workflow ends up with `gh
issue create` inlined, and swapping means editing all of them. The abstraction
here is deliberately thin — five verbs — and exists to keep that one decision
reversible, not to wrap a tracker's feature set.

    open    file a ticket, or update the existing one with the same KEY
    note    add a comment to the ticket with that KEY
    close   close it, with a reason
    assign  set the owner — this is the CLAIM, see ADR 0012
    list    what is open

THE list() CONTRACT, because a caller cannot guess it: every backend returns
dicts with `number`, `key`, `title`, `body`, `labels`, `assignees`,
**oldest first**. `key` is there because every other verb takes a key, so a
caller that lists and then acts would otherwise have to re-derive it.
Ordering and body are part of the interface, not an implementation detail — the
first consumer assumed both and got them wrong in opposite directions per
backend, which is what a seam is supposed to prevent.

IDEMPOTENCY IS THE POINT. An agent re-runs; a scheduled lane fires nightly; a
flaky job fails five times. Without a stable key each of those becomes a new
ticket, and a tracker full of duplicates gets ignored — the same way a noisy
alert channel does. Every ticket carries `<!-- ticket-key: KEY -->` in its body,
and `open` on an existing key EDITS rather than creates.

SWAPPING. Pick with TICKET_BACKEND (or `--backend`):

    github   GitHub Issues via the gh CLI          (default)
    file     newline-delimited JSON on disk        (offline, tests, air-gapped)
    none     accept and discard, loudly            (explicitly off)

A backend is a class with open/note/close/assign/list. Adding one — Linear,
Jira, a webhook — means writing those five methods and registering it in
BACKENDS. It does NOT mean touching any caller.

    python scripts/ticket.py open --key ci/quality/flaky-x \\
        --title "quality: flaky test" --body-file diag.md --label ci
    python scripts/ticket.py note --key ci/quality/flaky-x --body "still failing"
    python scripts/ticket.py close --key ci/quality/flaky-x --reason "fixed in abc123"
    python scripts/ticket.py list --label ci

Pure stdlib. The github backend shells out to `gh`, which is already the auth
path everywhere else here — no token handling of our own.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, subprocess, sys

KEY_MARK = "<!-- ticket-key: {} -->"


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


# --------------------------------------------------------------------- github

class GitHubBackend:
    """GitHub Issues via `gh`. The auth path already used everywhere here."""

    name = "github"

    def __init__(self, repo=None):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY") or ""
        self.gh = shutil.which("gh") or next(
            (c for c in (r"C:\Program Files\GitHub CLI\gh.exe",
                         r"C:\Program Files (x86)\GitHub CLI\gh.exe")
             if os.path.exists(c)), None)

    def available(self):
        if not self.gh:
            return False, "gh CLI not found on PATH"
        if not self.repo:
            return False, "no repo: set GITHUB_REPOSITORY or pass --repo"
        return True, ""

    def _gh(self, *args):
        return _run([self.gh, *args, "--repo", self.repo])

    def _find(self, key):
        """The issue carrying this key, or None. Searches OPEN and CLOSED.

        Closed ones matter: re-filing a ticket someone deliberately closed
        should reopen the conversation, not start a second one that loses it.
        """
        r = self._gh("issue", "list", "--state", "all", "--limit", "200",
                     "--json", "number,body,title,state")
        if r.returncode != 0:
            return None
        try:
            for i in json.loads(r.stdout or "[]"):
                if KEY_MARK.format(key) in (i.get("body") or ""):
                    return i
        except ValueError:
            return None
        return None

    def open(self, key, title, body, labels=()):
        marked = f"{body}\n\n{KEY_MARK.format(key)}"
        found = self._find(key)
        if found:
            r = self._gh("issue", "edit", str(found["number"]), "--body", marked)
            if r.returncode != 0:
                return False, r.stderr.strip()[:300]
            if found.get("state") == "CLOSED":
                self._gh("issue", "reopen", str(found["number"]))
                return True, f"reopened #{found['number']} (was closed)"
            return True, f"updated #{found['number']}"
        cmd = ["issue", "create", "--title", title, "--body", marked]
        for l in labels:
            cmd += ["--label", l]
        r = self._gh(*cmd)
        if r.returncode != 0:
            # A missing label is the common first-run failure and is not worth
            # losing the ticket over — retry without labels and say so.
            if labels and "label" in (r.stderr or "").lower():
                r2 = self._gh("issue", "create", "--title", title, "--body", marked)
                if r2.returncode == 0:
                    return True, (r2.stdout.strip() +
                                  f"  (labels {','.join(labels)} do not exist; filed without)")
            return False, r.stderr.strip()[:300]
        return True, r.stdout.strip()

    def note(self, key, body):
        found = self._find(key)
        if not found:
            return False, f"no ticket with key {key}"
        r = self._gh("issue", "comment", str(found["number"]), "--body", body)
        return (r.returncode == 0), (r.stdout.strip() or r.stderr.strip()[:300])

    def close(self, key, reason=""):
        found = self._find(key)
        if not found:
            return False, f"no ticket with key {key}"
        args = ["issue", "close", str(found["number"])]
        if reason:
            args += ["--comment", reason]
        r = self._gh(*args)
        return (r.returncode == 0), (r.stdout.strip() or r.stderr.strip()[:300])

    def assign(self, key, who):
        """Claim a ticket for an agent, as a LABEL — and verify it landed.

        NOT the GitHub assignee, for two reasons found by testing rather than
        assuming:

        1. **Assignees must be real accounts.** `repos/:o/:r/assignees` lists
           only actual collaborators. An agent persona is not one, and per
           NAMING.md rule 5 a persona is "a human-facing label ... never used as
           a technical key" — which is literally what this makes it.
        2. **`gh` exits 0 on a rejected assignee.** It prints "Could not resolve
           to a user" to stderr and returns 0, so a returncode check reports
           SUCCESS while assigning nobody. Under ADR 0012 that is not cosmetic:
           the claim IS the mutual exclusion, so a silently-failed claim means
           every poll re-serves the same ticket and N agents do one job — the
           exact duplicate execution the design exists to prevent.

        Hence: label, then READ IT BACK. "I ran the command" is not "the ticket
        was claimed", and this is the one place that distinction costs
        correctness rather than tidiness.
        """
        found = self._find(key)
        if not found:
            return False, f"no ticket with key {key}"
        label = f"agent:{who}"
        num = str(found["number"])
        # --add-label creates nothing; the label must exist first. Idempotent,
        # and a failure here is not fatal — the add below is what we verify.
        self._gh("label", "create", label, "--color", "5319E7",
                 "--description", "Claimed by an agent (ADR 0012)")
        self._gh("issue", "edit", num, "--add-label", label)
        r = self._gh("issue", "view", num, "--json", "labels")
        try:
            names = {l.get("name") for l in json.loads(r.stdout or "{}").get("labels", [])}
        except ValueError:
            names = set()
        if label not in names:
            return False, (f"claim did NOT land — {label} is not on #{num} after "
                           f"the edit. Refusing to report success.")
        return True, f"claimed #{num} as {label}"

    def list(self, label=None):
        # body and assignees are part of the contract; `gh` omits both unless
        # asked. Without body a caller cannot read a handoff; without assignees
        # it cannot tell claimed work from claimable.
        cmd = ["issue", "list", "--state", "open", "--limit", "100",
               "--json", "number,title,body,labels,assignees"]
        if label:
            cmd += ["--label", label]
        r = self._gh(*cmd)
        if r.returncode != 0:
            return []
        try:
            rows = json.loads(r.stdout or "[]")
        except ValueError:
            return []
        # The key lives in the body marker; surface it so a caller that lists
        # and then acts does not have to re-parse the marker itself.
        for row in rows:
            m = re.search(r"<!-- ticket-key: (.+?) -->", row.get("body") or "")
            row["key"] = m.group(1) if m else None
            # The CLAIM is an `agent:` label, not the GitHub assignee — see
            # assign(). `assignees` in the contract means "who holds this",
            # which for an agent fleet is the label. A human assignee still
            # shows in GitHub's UI and means what it always meant: accountable.
            row["assignees"] = [l["name"].split(":", 1)[1]
                                for l in (row.get("labels") or [])
                                if l.get("name", "").startswith("agent:")]
        # gh returns newest-first; the contract is oldest-first, because a queue
        # that serves the newest item starves the oldest.
        return sorted(rows, key=lambda r: r.get("number", 0))


# ----------------------------------------------------------------------- file

class FileBackend:
    """Newline-delimited JSON on disk.

    Not a toy: it is what makes the abstraction honest. A seam with one
    implementation is not a seam, it is an assumption — and this one runs
    offline, in tests, and anywhere the tracker is unreachable.
    """

    name = "file"

    def __init__(self, path=None):
        self.path = path or os.environ.get("TICKET_FILE") or ".tickets.jsonl"

    def available(self):
        return True, ""

    def _all(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except (OSError, ValueError):
            return []

    def _save(self, rows):
        with open(self.path, "w", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    def open(self, key, title, body, labels=()):
        rows = self._all()
        for r in rows:
            if r["key"] == key:
                r.update(title=title, body=body, labels=list(labels), state="open")
                self._save(rows)
                return True, f"updated {key}"
        rows.append({"key": key, "title": title, "body": body,
                     "labels": list(labels), "state": "open", "notes": []})
        self._save(rows)
        return True, f"created {key}"

    def note(self, key, body):
        rows = self._all()
        for r in rows:
            if r["key"] == key:
                r.setdefault("notes", []).append(body)
                self._save(rows)
                return True, f"noted on {key}"
        return False, f"no ticket with key {key}"

    def close(self, key, reason=""):
        rows = self._all()
        for r in rows:
            if r["key"] == key:
                r["state"] = "closed"
                if reason:
                    r.setdefault("notes", []).append(f"closed: {reason}")
                self._save(rows)
                return True, f"closed {key}"
        return False, f"no ticket with key {key}"

    def assign(self, key, who):
        rows = self._all()
        for r in rows:
            if r["key"] == key:
                # Mirrors the GitHub backend: the claim is an agent: label, and
                # `assignees` is derived from it so the list() contract holds
                # identically across backends.
                labs = [l for l in r.get("labels", []) if not l.startswith("agent:")]
                labs.append(f"agent:{who}")
                r["labels"] = labs
                self._save(rows)
                return True, f"claimed {key} as agent:{who}"
        return False, f"no ticket with key {key}"

    def list(self, label=None):
        # Insertion order IS oldest-first here; the contract is satisfied without
        # sorting, and saying so stops someone "fixing" it with a reverse().
        return [{"number": i, "title": r["title"], "body": r.get("body", ""),
                 "labels": r.get("labels", []),
                 "assignees": [l.split(":", 1)[1] for l in r.get("labels", [])
                               if l.startswith("agent:")],
                 "key": r["key"]}
                for i, r in enumerate(self._all(), 1)
                if r.get("state") == "open" and (not label or label in r.get("labels", []))]


# ----------------------------------------------------------------------- none

class NoneBackend:
    """Accept and discard — but LOUDLY.

    The failure this prevents: a tracker misconfigured to silence, where every
    agent believes it filed a ticket and nothing exists. Turning ticketing off
    is a legitimate choice; doing it invisibly is not.
    """

    name = "none"

    def available(self):
        return True, ""

    def _noop(self, verb, key):
        print(f"  ticket backend is 'none' — {verb} {key} DISCARDED, nothing was "
              f"filed anywhere.", file=sys.stderr)
        return True, "discarded (backend=none)"

    def open(self, key, title, body, labels=()):
        return self._noop("open", key)

    def note(self, key, body):
        return self._noop("note", key)

    def close(self, key, reason=""):
        return self._noop("close", key)

    def assign(self, key, who):
        return self._noop("assign", key)

    def list(self, label=None):
        return []


BACKENDS = {"github": GitHubBackend, "file": FileBackend, "none": NoneBackend}


def get_backend(name=None, repo=None):
    name = (name or os.environ.get("TICKET_BACKEND") or "github").strip().lower()
    if name not in BACKENDS:
        raise SystemExit(f"unknown backend {name!r}; known: {', '.join(sorted(BACKENDS))}")
    b = BACKENDS[name](repo) if name == "github" else BACKENDS[name]()
    return b


def main(argv=None):
    ap = argparse.ArgumentParser(description="File and update tickets (swappable backend).")
    ap.add_argument("verb", choices=["open", "note", "close", "assign", "list"])
    ap.add_argument("--key", help="stable identity; re-running with the same key UPDATES")
    ap.add_argument("--title")
    ap.add_argument("--body")
    ap.add_argument("--body-file")
    ap.add_argument("--reason", default="")
    ap.add_argument("--label", action="append", default=[])
    ap.add_argument("--backend", default=None)
    ap.add_argument("--repo", default=None)
    ap.add_argument("--assignee", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    b = get_backend(a.backend, a.repo)
    ok, why = b.available()
    if not ok:
        # Refuse rather than fall back to a different backend. A silent
        # downgrade means an agent reports "filed" while the ticket landed
        # somewhere nobody reads.
        print(f"ticket: backend {b.name!r} unavailable — {why}", file=sys.stderr)
        return 2

    body = a.body or ""
    if a.body_file:
        try:
            body = open(a.body_file, encoding="utf-8").read()
        except OSError as e:
            print(f"ticket: cannot read {a.body_file} ({type(e).__name__})", file=sys.stderr)
            return 2

    if a.verb == "list":
        rows = b.list(a.label[0] if a.label else None)
        print(f"{len(rows)} open ticket(s) [{b.name}]")
        for r in rows:
            labs = ",".join(l.get("name", l) if isinstance(l, dict) else str(l)
                            for l in (r.get("labels") or []))
            print(f"  #{r.get('number')}  {r.get('title', '')[:70]}" + (f"  [{labs}]" if labs else ""))
        return 0

    if not a.key:
        ap.error("--key is required (it is what makes a re-run update instead of duplicate)")
    if a.dry_run:
        print(f"[dry-run] {b.name}: would {a.verb} {a.key}")
        return 0

    if a.verb == "open":
        if not a.title:
            ap.error("--title is required to open")
        ok, detail = b.open(a.key, a.title, body, a.label)
    elif a.verb == "note":
        ok, detail = b.note(a.key, body)
    elif a.verb == "assign":
        if not a.assignee:
            ap.error("--assignee is required to assign")
        ok, detail = b.assign(a.key, a.assignee)
    else:
        ok, detail = b.close(a.key, a.reason)

    print(f"  {b.name}: {'ok' if ok else 'FAILED'} — {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
