# n8n workflow scaffolds

Import these in n8n (Workflows → Import from File). They're **starting scaffolds**
— after importing, validate them with the runner's `n8n` MCP tools
(`validate_workflow`) before relying on them, and adjust node `typeVersion`s if
your n8n is older/newer.

## Changing a workflow's ID orphans its webhook

**Read this before renaming or re-importing anything with a webhook node.**

n8n registers webhooks in its own `webhook_entity` table, keyed by the *workflow
ID*. Change the ID and re-import, and n8n treats it as a new workflow — the old
row is never deregistered, so it outlives its owner and keeps squatting the path.
The new workflow then fails to activate with:

> The URL path that the "POST /ask-human" node uses is already taken.

That message points at the new workflow, which is the one thing that is not
wrong. The repo can be perfectly clean — exactly one workflow declaring the path,
the right one — and the collision is entirely in live state.

It happened here: renaming the workflow IDs left the OLD id still holding
`POST /ask-human`, so the ask-human door returned **404** and any run that needed
to reach a human silently could not. (Writing the old id into this sentence is
what the public-hygiene gate caught on the first attempt — the point survives
without it.)
An orphan **survives a restart**, and it cannot be cleared from the UI because
its owning workflow no longer exists — there is nothing to open.

```bash
python scripts/n8n_doctor.py     # detects orphans, collisions, and unserved paths
```

Run it after any workflow rename or bulk re-import. It reports the remedy rather
than applying it: clearing an orphan writes to n8n's database, which must be done
with n8n **stopped**.

## The notification seam (Slack / Teams / Email — user's choice)

`notify.subworkflow.json` is the single place a channel is chosen. Everything
that needs to alert calls it via **Execute Workflow** with:

```json
{ "severity": "error|warn|info", "title": "…", "message": "…", "source": "…" }
```

It routes on the **`NOTIFY_CHANNEL`** env var (`slack` | `teams` | `webhook`;
falls back to the generic webhook) and reads the destination from whichever of
these you set (in `.env`, passed to the n8n container):

| Channel | Env var | Notes |
|---|---|---|
| `slack` | `SLACK_WEBHOOK_URL` | Slack Incoming Webhook. Body `{text}`. |
| `teams` | `TEAMS_WEBHOOK_URL` | Sends an Adaptive Card (works with Teams "Workflows" webhooks). |
| `webhook` | `NOTIFY_WEBHOOK_URL` | Generic POST — bridge to PagerDuty, Discord, email relay, anything. |

To add a channel, add one branch to the Switch and one env var — callers don't
change. That's the abstraction: **swap the channel, not the workflows.**

### Wiring it up

1. Import `notify.subworkflow.json`, open it, copy its **workflow ID** (from the
   URL).
2. Import `error-trigger.workflow.json`, open the **Notify** node, and set it to
   the notify workflow (replace `REPLACE_WITH_NOTIFY_WORKFLOW_ID`).
3. On each real workflow: **Settings → Error Workflow → Global Error Handler**.

## Parallel worktree fan-out (AG-2)

`parallel-fanout.example.json` orchestrates several agents at once, each isolated
in **its own git worktree** on branch `agent/<id>`, each running a **self-verify**
shell command (tests/build/lint). It calls the runner's `POST /fanout`.

Prerequisites and behavior:
- There must be a **git repo** at `workspace/<repo>` with the `base` branch
  committed (worktrees fork from it). Set `repo`/`base` in the Code node.
- Each task: `{ id, prompt, verify?, model?, maxTurns? }`. `verify` is a shell
  command run inside that worktree; its pass/fail is reported.
- **Nothing is merged or pushed.** Review the branches (`git log agent/<id>`) or
  the patches written to `workspace/.fanout/<id>.patch`, then
  `git merge agent/<id>` the ones you want. Merge stays a human act.
- `clean:true` makes it re-runnable (wipes same-id worktrees/branches first).
- Concurrency is bounded by `FANOUT_CONCURRENCY` (default 4).
- Pipe the final `Summarize for review` output into the **Notify** sub-workflow
  for an alert when the batch finishes.

## Drive → workspace sync (scrubbed ingest)

`drive-sync.workflow.json` makes the GSuite story real: every 15 minutes it pulls
changed files from a scope's Drive folder and lands them — **tokenized** — in
`workspace/scopes/<chain>/ingest/`. Content enters the agent workspace ONLY
through the scrubber.

Setup:
1. In n8n, create a **Google Drive OAuth2** credential and attach it to the
   *List changed files* and *Download text* nodes (they use the predefined
   credential type against the Drive REST API — stabler than the Drive node's
   changing schemas).
2. In the *Config + last sync* Code node set `FOLDER_ID` (the Drive folder or
   shared-drive folder feeding this scope) and `SCOPE` (a node from
   `context/scopes.json`). **One folder → one scope per workflow** — duplicate
   the workflow per mapping so failures stay isolated.
3. Set the scrubber token in the *Scrub + ingest* node.
4. **Activate** the workflow — the incremental watermark (`lastSync`) only
   persists on active schedule runs; a manual test run re-syncs the last 7 days.

Behavior and guarantees:
- Google Docs export as text; text/markdown/JSON/XML download raw; binaries and
  media are **skipped explicitly** and listed in the summary (no silent gaps).
- The scrubber tokenizes emails/phones/SSNs/cards deterministically
  (`«EMAIL_1»` — same value, same token) and stores maps in `vault/<scope>.json`,
  which **no runner ever mounts**. Names/addresses need NER — swap the scrubber
  backend to Cloud DLP or Presidio for that class (see GSUITE-GCP.md).
- **Silo scopes are refused** by the shared scrubber — their data must never
  touch the shared workspace; run a scrubber+workspace pair inside their
  dedicated runner's mounts instead.
- Pipe *Watermark + summary* into the Notify sub-workflow for a per-sync alert.

## Deliverables → Drive (the outbound half)

`deliverables.workflow.json` closes the loop. Agents write **tokenized** output
to `workspace/scopes/<chain>/deliverables/`; every 15 minutes this workflow:

1. `POST scrubber /outbox` — lists pending files, **rehydrated** from the vault
   (PII re-enters here, post-model — the model never saw real values).
2. Uploads each to the scope's Drive folder (multipart, built deterministically
   in a Code node; attach your Google Drive OAuth2 credential to the Upload node).
3. `POST scrubber /outbox/ack` — the tokenized original archives to
   `deliverables/sent/<timestamp>__<name>` so nothing ships twice. Ack runs
   *after* a successful upload (at-least-once, never lost).

Setup mirrors drive-sync: set `SCOPE` + `FOLDER_ID` in the Config node, scrubber
token on the two scrubber nodes, one scope→folder per workflow copy.

Notes:
- Unknown tokens (`«EMAIL_9»` with no vault entry) are left visible rather than
  silently dropped — a loud artifact beats a quietly wrong document.
- **Human gate built in:** the batch goes through the approval gate before
  upload. `requireApproval: true` in Config (keep it on for tenant-facing or
  `pii: block` scopes); `false` flips the gate to auto for internal scopes.
  Rejection/timeout ships nothing — files stay pending and reappear next cycle.
- Silo scopes are refused by the shared scrubber here too; their deliverables
  ship via their dedicated pair.

## Approval gate (reusable sub-workflow)

`approval-gate.subworkflow.json` — call via Execute Workflow with
`{ title, message, source, autoApprove? }`; returns `{ approved, auto, timedOut }`.

- Sends the request through the **Notify** seam (set the Notify workflow ID in
  its *Notify approvers* node), with ✅/❌ **links** built from the execution's
  resume URL — channel-agnostic, works in Slack/Teams/Chat/email alike.
- A **Wait** node pauses the execution until a link is clicked. **Timeout (24h)
  = rejected — default deny.** Anything but an explicit `approved=true` is a no.
- `autoApprove: true` short-circuits to approved so callers keep one linear flow
  and decide per scope whether a human is required.
- Import it, copy its workflow ID, and set that ID in the *Request approval*
  nodes of `deliverables` and `promote-skill`.
- n8n must know its public URL (`WEBHOOK_URL` env) for resume links to be
  clickable from chat; on localhost the links work from the same machine.

## Ask a human (agent question gate)

`ask-question.subworkflow.json` — call via Execute Workflow with
`{ question, context?, source?, timeoutHours? }`; returns
`{ answered, answer, timedOut, note }`.

The gap it closes: our controls were **halt** (kill switch, all-or-nothing) and
**approve** (binary, *before* an action). Neither lets an agent say *"I don't know
this, and guessing would be wrong."* Without it, a blocked agent guesses.

- Notifies through the same **Notify** seam, written to the message contract —
  the title states what is blocked, the action line is the link to answer.
- A **Wait** node in `resume: form` mode serves an n8n form; the human types an
  answer and submitting it resumes the run.
- ⚠️ The link must be **`$execution.resumeFormUrl`**, not `$execution.resumeUrl`.
  The form is served at `/form-waiting/<id>`; `resumeUrl` points at
  `/webhook-waiting/<id>`, which does not 404 — it **resumes the wait with no
  submitted data**. A human clicking the wrong link would unblock the agent while
  answering nothing.
- An empty `question` **throws** rather than rendering a form that asks nothing.

**`answered` is separate from `answer`, deliberately.** "Nobody answered" and
"answered with nothing" must never collapse into one value — a caller that cannot
tell them apart treats silence as an empty-but-valid fact and carries on, which
is the guessing this control exists to stop. A timeout returns `timedOut: true`,
and `note` says which case occurred so the caller can log *why* it proceeded.

### Why this is a sibling of the approval gate, not a mode inside it

The gate's timeout is a **safety invariant**: anything that is not an explicit
`approved=true` is a rejection, silence included. A question's timeout means
"nobody answered", which denies nothing. Routing both through one decision node
is one wrong comparison away from turning silence into approval — so default-deny
stays unconditional where it matters.

### Answering in Slack (`scripts/slack_ask.py`)

The n8n form works but means leaving Slack. `slack_ask.py` posts the question with
`chat.postMessage`, watches the thread, and delivers the first human reply to the
router's `/ask/answer` — **the same endpoint the form path uses**. The runner's
mailbox is read-once, so whichever answer lands first wins and the other is
discarded. Two channels, one answer, no coordination.

**Why polling and not the Events API.** Slack can only push to a URL it can
reach, and this stack is loopback-only (ADR 0009). Socket Mode would avoid that
with an outbound WebSocket but needs a WebSocket tenant, and `lanes` is
stdlib-only on purpose. `conversations.replies` needs nothing but outbound
HTTPS, which `lanes` already does. The cost is that a reply is seen within one
poll interval instead of instantly.

**A bot token is required — the incoming webhook cannot do this.**
`SLACK_WEBHOOK_URL` is send-only and does not even return the message `ts`, and
without a `ts` there is no thread to watch. Setup:

1. Create a Slack app → **OAuth & Permissions** → add bot scopes `chat:write`
   and `channels:history` (`groups:history` for a private channel).
2. Install to the workspace; copy the bot token (`xoxb-…`).
3. **Invite the bot to the channel** (`/invite @yourbot`) — without this every
   call fails `not_in_channel`.
4. Copy the channel ID (channel → View channel details → bottom).
5. Put `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` in `.env`.

```bash
python scripts/slack_ask.py --ticket <uuid> --question "Which folder?" --minutes 10
```

It **fails closed and names the missing variable** rather than pretending.

**Not yet wired into the gate, deliberately.** `lanes` runs a single-threaded
`HTTPServer`, so a ten-minute blocking watch there would freeze the digest, eval,
retention and research lanes with it. Wiring this needs either
`ThreadingHTTPServer` or a separate process — a real decision, not a line of
glue, so it is not being made implicitly.

**Verified:** the watcher logic is covered by `tests/slack_ask_test.py`,
including the failure that matters most — it must never read its own posted
question as the human's reply. **Unverified:** every live Slack call. Nothing
here has spoken to Slack, because no bot token exists yet.

**Not yet wired:** an agent cannot *raise* a question from inside a run. Today a
workflow calls this gate; making the runner emit a question mid-execution is the
follow-up.

## Promote skill (scrub → review → commit)

`promote-skill.workflow.json` — the only sanctioned path for knowledge to move
**up** the scope tree (tenant → practice, team → department, etc.):

1. `POST scrubber /promote/preview` — returns the skill **scrubbed** via the
   source scope's vault; the scrubber refuses any target that isn't a strict
   ancestor.
2. The approval gate shows the reviewer the full scrubbed content inline.
   **Promotions are never auto-approved** — widening visibility is always a
   human act. Reviewers should also generalize: the scrub catches structured
   PII, not tenant-recognizable specifics (project names, unusual numbers).
3. On approval, `POST /promote/commit` writes it to the target scope's `skills/`
   with a provenance header — and **re-checks for raw PII**, refusing the commit
   if any reappeared.

## Incident responder (event-driven skills, AG-3)

`incident-responder.workflow.json` is the four-step on-call maturity model as a
lane: **event → skill-guided diagnosis → human-gated fix → learning encoded**.

Point any of these at `POST <n8n>/webhook/incident` — the normalizer detects the
payload shape automatically:
- **PagerDuty** (v3 webhook subscription)
- **Prometheus Alertmanager / Grafana** (`alerts[]` payloads)
- **Slack slash command** (`/incident <description>` → set the command's request
  URL to the webhook; n8n's immediate 200 satisfies Slack's 3-second rule).
  For Events-API @mentions you must also answer Slack's one-time
  `url_verification` challenge — slash commands avoid that entirely.
- **Anything else** via generic JSON `{ title, description, severity?, service? }`

Flow per event:
1. **Normalize + classify** — service→scope mapping and keyword→skill-category
   rules live in the Config section of the first Code node.
2. **Diagnose** via the router (`task_type: debug`, scoped cwd) — the agent
   consults the scope's `skills/` (plus inherited ancestor skills) and writes a
   report to `deliverables/<id>.md`. **It proposes; it does not touch anything.**
3. **Approval gate** — a human sees the diagnosis excerpt and decides whether the
   fix gets applied. Timeout/reject = diagnosis-only, report kept.
4. **Apply via `/fanout`** — the fix lands in an isolated worktree with
   self-verify (`applyVerify` command); **merge stays a human act**, exactly as
   in the AG-2 lane.
5. **Encode learning** — a second, cheap routed task writes the incident's
   lesson to `skills/<category>-<id>.md` (no tenant names, no PII), so the next
   occurrence starts from knowledge instead of zero.

Setup: set the approval-gate workflow ID, router/runner tokens, and the Config
maps; requires the same git repo under `workspace/` as the fan-out workflow.

## Dependency bumps (scheduled lane, AG-3)

`dependency-bumps.workflow.json` — the second AG-3 lane, and the template for
every future scheduled lane (cleanup sweeps, lint debt, doc rot): **Schedule →
`/fanout` → summarize → Notify.**

Weekly (Mon 06:00) it fans three tasks into isolated worktrees:
- `security-bumps` — advisory-driven upgrades only, verified by
  `npm audit --audit-level=high && npm test`
- `minor-bumps` — minor/patch upgrades, single-package revert on breakage
- `stale-report` — a majors report (`UPGRADES.md`) with migration risk, no changes

Design note — **why there's no approval gate before the run**: the ladder's rule
is to match the guardrail to the lane. This lane produces *branches*, never
merges — nothing irreversible happens, so a pre-run gate would be pure friction.
**The human gate IS the merge** (`git merge agent/<id>` on the green ones).
`clean: true` makes it re-runnable: last week's unmerged branches are replaced,
so the lane never accumulates stale worktrees.

Setup: set repo/base/verify commands in the Config node (npm assumed — swap for
pnpm/pip/cargo), the runner token, and the Notify workflow ID; activate.

## Retention sweep (scheduled lane)

`retention-sweep.workflow.json` — the third scheduled lane: **Schedule → Execute
Command → report.** Daily (06:00) it runs `scripts/retention_sweep.py --apply`,
which ages out `ingest`/`deliverables` files past each scope's effective
`retentionDays` and conservatively prunes orphaned token-vault entries (see
[../RETENTION.md](../docs/security/RETENTION.md)).

Ships **inactive**: review a dry-run first (drop `--apply`), confirm the report,
then set `active: true`. Adjust the working directory / interpreter to your deploy
(the container path `/app` is shown). It never touches `audit/`. **Offboarding is
deliberately not scheduled** — `scripts/offboard_scope.py --confirm` is a
human-invoked action when a tenant leaves, and it writes a deletion certificate to
`audit/deletions/`.

## Research watch (scheduled lane)

`research-watch.workflow.json` — the first lane that actually **hands work to an
agent**: **Schedule (Mon 07:30) → lanes `/research` → triage → IF → router
`/route` → Notify.** It watches releases in the six projects tied to a *recorded
decision* and asks the agent which, if any, of those decisions actually moves.

Two properties are load-bearing:

- **The fetch and the reasoning are separate containers.** The agent runners are
  firewalled default-deny (verified: `github.com` times out from `claude-runner`,
  while `api.anthropic.com` returns 401). So `lanes` — which has egress — does the
  network call, and the runner reasons over the JSON it returns. Same split as
  drive-sync. Widening the runner's allowlist so it could browse would give back
  the containment the firewall exists for.
- **`action: "advise"`.** The lane may only *recommend* a re-pin. **That is a
  declaration, not yet a restriction** (threat-model H5): the mode cap reads the
  declared verb, and the scope declares no `allowedTools`, so the agent has the
  runner's full toolset, Bash included. A confused or compromised run *could*
  change what we run. The router half of H5 binds a request's tools to its declared
  action; until `risk.enforce.actionBinding` is `block`, treat this lane's output
  as advice and the lane itself as unrestricted.
- **`trigger: "scheduled"`.** It was `"schedule"`, which is not a key in
  `policy.json → risk.autonomy`, so the lane scored autonomy **1** — as if a
  human had started it — and `requireVerify` never fired. An unknown verb falls
  back to the least-risky default; that is the same bug class as H5, on the
  other caller-declared field. Re-import the workflow into n8n: fixing the file
  does not change the running lane.

The IF node exists for cost: on a quiet week no model call is made at all. The
lane still posts, because a lane that only speaks when it finds something is
indistinguishable from a lane that stopped running.

It also carries a **Run on demand** trigger. That is the on-demand tier from
`notification-taxonomy.md`, and it is the only way to test a scheduled lane:
`n8n execute` refuses a workflow whose sole trigger is a schedule, so a
schedule-only lane cannot be exercised until its schedule fires.

```bash
docker exec -e N8N_RUNNERS_BROKER_PORT=5699 n8n n8n execute --id=aicp-research-watch
```

(The `N8N_RUNNERS_BROKER_PORT` override is needed because the CLI starts a second
task broker that would otherwise collide with the running server on 5679.)

**Requires `ROUTER_TOKEN` in the n8n container** — see `docker-compose.yml`. It was
missing until this lane was run end to end, which meant *no* workflow could call
the router at all.

## Feedback → eval inbox (improvement loop)

`feedback.workflow.json` — a **scaffold** (inactive) that closes the agent
improvement loop from a human thumbs-down to a captured regression case, without
needing python inside n8n. A **Webhook** (or **Manual**) trigger receives
`{ scope, prompt, output, verdict, note }`; a **Code** node shapes it into a case
record; a **convertToFile → Write file** pair drops it to `eval/inbox/<id>.json`.

An operator then promotes the inbox with
`python scripts/eval_capture.py --dataset <name> --ingest eval/inbox --scrubber-url … --scrubber-token …`
(the prompt is **tokenized via the scrubber** at that step, so raw PII never lands
in a dataset) and gates with `python scripts/eval_run.py --dataset <name>`.

- **Mount `./eval` into the n8n container** (e.g. `./eval:/data/eval`) so the Write
  node can reach `eval/inbox/`. `eval/inbox/` is `.gitignored` (it may hold raw
  tenant content until eval_capture tokenizes it) — terminate the webhook inside
  the trusted network and treat the inbox as sensitive.
- Ships **inactive**. See [../IMPROVEMENT-LOOP.md](../docs/design/IMPROVEMENT-LOOP.md) and
  [../EVAL.md](../docs/design/EVAL.md).

## Global Error Handler

`error-trigger.workflow.json` is the middle layer of the three-layer error
strategy (node retries → **global Error Trigger** → data-quality checks). It only
fires on *production* executions (webhook/schedule/called), never on manual
"Execute Workflow" runs. Keep it minimal — capture → notify — and push any richer
logic into a separate workflow triggered by the notification.
