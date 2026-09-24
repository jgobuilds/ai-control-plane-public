# AI Control Plane — Quickstart & operator runbook

Stand up the control plane, prove it works, and run it day to day.

> **Status: first real run.** Everything is validated structurally and by the
> Python test suites, but this stack has **not been run in a container yet**. The
> first `docker compose build`/run will surface the known unknowns flagged in
> **§8 Troubleshooting** (in-container OAuth, the exact Gemini CLI flags, n8n node
> versions). Do the smoke test in §2 before trusting it, and read
> **§7 Before real tenant data** before any real engagement.

> **Overview & capabilities.** For the visual architecture diagram and the full
> capabilities overview, open
> [`docs/design/architecture.html`](docs/design/architecture.html) in a browser
> (self-contained, theme-aware), or read
> [`docs/design/ARCHITECTURE.md`](docs/design/ARCHITECTURE.md) on GitHub. Both are
> GENERATED from the running stack by `python scripts/gen_architecture.py`, so
> they show what is actually deployed rather than what was once intended. A quick
> textual version of the architecture is in **§0** just below.

---

## Fast path — the installer

The installer answers the big architectural decisions for you, writes the config,
and can build + start the stack.

```sh
python scripts/install.py            # interactive
# wrappers:  ./install.sh            |   .\install.ps1   (Windows PowerShell)
```

It prompts for:

| Prompt | Options | Default |
|---|---|---|
| Providers | `claude` / `claude+gemini` | claude+gemini |
| Notification channel (+ webhook URL) | `slack` / `teams` / `webhook` | webhook |
| Scope hierarchy | `keep` / `solo` / `corp` / `consulting` | keep |
| PII detection | `heuristic` / `presidio` / `dlp` | heuristic |
| Local-dev no-auth | `yes` / `no` | no |

The three tokens (`RUNNER` / `ROUTER` / `SCRUBBER`) are generated for you — three
distinct secrets, reusing any already present in `.env`. It then writes `.env` +
`context/scopes.json`, regenerates the derived artifacts (`compose.scopes.yml`,
the use-case register, the compliance map), and runs the conformance suite.

Flags:
- `--dry-run` — print the plan, write nothing.
- `--non-interactive` — accept defaults + any value flags (CI / repeatable).
- `--up` — also run `docker compose build` + `up -d` at the end.
- Example, fully scripted:
  ```sh
  python scripts/install.py --non-interactive --scope-template consulting \
    --notify-channel slack --slack-url https://hooks.slack.com/… --up
  ```

Reruns are safe: existing tokens are reused and `.env` is backed up to `.env.bak`.
After it runs, do the one-time **CLI logins (§2.3)** and the **smoke test (§2.6)**.
Prefer to configure by hand? Skip the installer and follow §2 onward.

---

## 0. What you're standing up

```
you ──► n8n (localhost:5678) ──/route──► router ──► claude-runner / gemini-runner ──► vendor CLIs
                                  │                        (firewalled, own login)
                                  ├─► scrubber (PII tokenize/rehydrate; owns the vault)
                                  └─► status.html (python scripts/gen_status.py)
```

| Service | Port | Purpose |
|---|---|---|
| `n8n` | `127.0.0.1:5678` | orchestration UI + workflows (your front door) |
| _(status view)_ | `status.html` | `python scripts/gen_status.py` — gates, containers, lanes, ledger, agent sessions |
| `router`, `claude-runner`, `gemini-runner`, `scrubber` | **internal only** | no host ports — reachable only inside the docker network |

The **router** is the single policy entry point. The runners talk only to their
vendor. The **scrubber** is the only container that mounts the PII vault.

---

## 1. Prerequisites

- **Docker Desktop** with the **WSL2 backend** (Windows) — running.
- **git**, and **Python 3.10+** on the host (the governance/ops scripts are
  host-side Python; stdlib + `pyyaml` — `pip install pyyaml`).
- A **Claude subscription** (Pro/Max) for `claude-runner`, and optionally a
  **Google account** for `gemini-runner` (free tier). Everything else is optional.

---

## 2. First-time setup

Run from the repo root (`D:\code\ai-control-plane`).

### 2.1 Secrets — three distinct tokens
```sh
cp .env.example .env
```
Edit `.env` and set **four different** random secrets (services refuse to start
without them):
```sh
# generate four: openssl rand -hex 32   (run it 4×, paste each)
RUNNER_TOKEN=<secret-1>
ROUTER_TOKEN=<secret-2>
SCRUBBER_TOKEN=<secret-3>
ASK_HUMAN_TOKEN=<secret-4>
```
> Local-dev only: set `ALLOW_NO_AUTH=1` to let services boot without tokens.
> **Never** use that with real data — every endpoint becomes unauthenticated.

### 2.2 Build
```sh
docker compose build
```

### 2.3 Log in the CLIs (the fiddly part — see §8)

**Claude — use `setup-token`, not `login`.** In-container `claude login` uses a
localhost OAuth callback that the host browser can't reach; `setup-token` avoids
it by issuing a long-lived token you capture. Run it from a **real terminal**
(needs a TTY), authenticate in your browser, and copy the printed
`sk-ant-oat01-…` token:
```sh
docker exec -it -u node -e HOME=/home/node claude-runner claude setup-token
```
Then put the token in `.env` and recreate the runner:
```sh
echo 'CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-…' >> .env   # gitignored; never committed
docker compose up -d claude-runner
```
The runner passes `CLAUDE_CODE_OAUTH_TOKEN` to `claude -p` for subscription auth.
(Verify: `docker exec -u node -e HOME=/home/node claude-runner claude -p "say ok"
--output-format json` returns `"is_error":false`.)

**Gemini — same pattern** (skip if Claude-only). Interactive `/auth` hits the same
in-container callback trap, so use a **free-tier AI Studio key** from
<https://aistudio.google.com/apikey>:
```sh
echo 'GEMINI_API_KEY=…' >> .env        # gitignored
docker compose up -d gemini-runner
```
Free tier, not metered billing. `generativelanguage.googleapis.com` is already in
the runner's egress allowlist, so no firewall edit is needed.

### 2.4 Start the core
```sh
docker compose up -d          # router + runners + scrubber + n8n
```
- n8n → http://localhost:5678  ·  status → `python scripts/gen_status.py --open`

### 2.5 Set up the n8n owner account
Open http://localhost:5678 and complete n8n's **owner-account** setup on first
launch. n8n is bound to localhost only, but the account keeps other local users
out. (For remote/SSO access, front it with IAP — see `GSUITE-GCP.md`.)

### 2.6 Smoke test — prove the router is enforcing

**(a) Deterministic path — no login needed.** `ping` hits a Tier‑0 rule and
returns `pong` without touching a model:
```sh
docker compose exec n8n sh -c \
 'wget -qO- --header="x-router-token: '"$ROUTER_TOKEN"'" \
   --header="content-type: application/json" \
   --post-data="{\"prompt\":\"ping\"}" http://router:8080/route'
# → {"decision":{"tier":"t0","deterministic":true,...},"result":{"result":"pong"},...}
```

**(b) A real model call — needs a CLI login.** A low-risk classify routes to the
cheapest tier:
```sh
docker compose exec n8n sh -c \
 'wget -qO- --header="x-router-token: '"$ROUTER_TOKEN"'" \
   --header="content-type: application/json" \
   --post-data="{\"prompt\":\"Reply with one word: ok\",\"task_type\":\"classify\"}" \
   http://router:8080/route'
```
If (a) returns `pong` you have auth + routing + policy working. If (b) returns a
`result`, the runner + CLI login work end to end.

---

## 3. Wire up the workflows (n8n)

In n8n → **Workflows → Import from File**, import from `n8n-workflows/`. Do these
three first (the others depend on them):

1. **Import `notify.subworkflow.json`**, open it, copy its **workflow ID** (from
   the URL). Pick your channel in `.env`: `NOTIFY_CHANNEL=slack|teams|webhook` +
   the matching `*_WEBHOOK_URL`, then `docker compose up -d` to reload.
2. **Import `approval-gate.subworkflow.json`**, set the Notify workflow ID inside
   it, copy its own ID.
3. **Import the consumers** (`deliverables`, `promote-skill`, `incident-responder`,
   `dependency-bumps`, `drive-sync`, `router-dispatch.example`, …). In each:
   - set the **approval-gate / notify workflow IDs** where referenced;
   - in every **HTTP Request** node, set the header token that matches the target:
     `/route` and `/fanout` (both on the router) → `ROUTER_TOKEN`, `scrubber/*` →
     `SCRUBBER_TOKEN`. A workflow never uses `RUNNER_TOKEN`: n8n does not hold it,
     and `boundary_check.py` fails any workflow that names it;
   - **Activate** scheduled lanes (drive-sync, dependency-bumps, eval-drift) — the
     watermark/schedule only runs when active.

For Drive lanes, create a **Google Drive OAuth2** credential and attach it to the
Drive nodes (see `n8n-workflows/README.md`).

---

## 4. Using it — the `/route` contract

Everything goes through **`POST http://router:8080/route`** (header
`x-router-token: $ROUTER_TOKEN`). Call it from an n8n HTTP node or the
`docker compose exec n8n …` pattern above.

Request:
```jsonc
{
  "prompt": "…",            // required
  "scope": "data-team",     // optional; a node in context/scopes.json (default: enterprise)
  "task_type": "architect", // optional; classify|generate|code-edit|plan|architect|review|…
  "action": "advise",       // advise|read|write|ingest|apply|send  (drives risk + mode caps)
  "trigger": "turn",        // turn|scheduled|proactive
  "verify": true,           // optional; adds a fresh-context verifier pass
  "goal": "…",              // success criteria for verify
  "approved": false,        // set true only after the approval gate has run
  "requester": "you@co",    // for approval RBAC / segregation of duties
  "allowFrontier": false    // lift the maxTier cost ceiling for this call
}
```
Response is `{ decision, result, verdict }` — or `{ decision, blocked: "…" }` when
a control stops it (`approval-required`, `halted`, `mode-forbids-action`,
`guardrail-input`, `approval-unauthorized`, …). `decision` is your audit line:
tier, model, scope, risk, mode, PII/guardrail findings.

**Examples**
- *Cheap classify:* `{"prompt":"categorize this ticket","task_type":"classify"}` → Tier‑1.
- *Frontier + verify:* `{"prompt":"design the schema","task_type":"architect","verify":true,"goal":"normalized, tenant-isolated"}`.
- *Acting request (gets gated):* `{"prompt":"apply the fix","action":"apply","trigger":"proactive"}` → `blocked:"approval-required"`; run it through the approval gate, then re-call with `approved:true` + `approver`.

**Parallel work:** `POST /fanout` on `claude-runner` runs several tasks, each in
its own git worktree, each self-verifying — you review diffs and merge by hand
(see `LADDER.md`).

---

## 5. Govern & operate (host-side)

Run these from the repo root with Python:

| Task | Command |
|---|---|
| **Halt everything now** | `python scripts/halt.py global "reason"` · resume: `python scripts/halt.py resume global` |
| Halt one scope | `python scripts/halt.py scope tenant-a "reason"` |
| Verify the audit ledger | `python scripts/verify_audit.py` |
| Quality / drift metrics (online) | `python scripts/eval_metrics.py --days 7` |
| Quality regression (offline eval) | `python scripts/eval_run.py --dataset eval/datasets/starter` — see IMPROVEMENT-LOOP.md |
| Capture a failure → eval case | `python scripts/eval_capture.py --dataset <name> --scope <s> --prompt @file …` |
| Run the governance tests | `python scripts/conformance_test.py` + `python tests/<name>_test.py` |
| Retention sweep (dry-run) | `python scripts/retention_sweep.py` · apply: `--apply` |
| Offboard a tenant (proof) | `python scripts/offboard_scope.py tenant-a --confirm --operator you` |

**When you change `context/scopes.json`, regenerate the derived artifacts:**
```sh
python scripts/gen_compose.py            # per-scope runner topology + runner-map.json
python scripts/gen_usecase_register.py   # docs/usecase-register.md + .html
python scripts/gen_compliance_map.py     # docs/compliance-map.md
python scripts/conformance_test.py       # must pass (ethical walls, mount isolation, …)
```

---

## 6. Optional add-ons

- **ML detection backends** (better PII/injection): set `SCRUB_BACKEND=presidio`
  (+ `SCRUB_BACKEND_URL`) and/or `policy.json → guardrails.backend`. Heuristic
  stays the default + fallback. See `DETECTION-BACKENDS.md`.
- **Capability factory** (generate a new CLI/MCP): `docker compose --profile
  builder run --rm claude-builder` — off by default; see
  [`docs/runbooks/CAPABILITY-FACTORY.md`](docs/runbooks/CAPABILITY-FACTORY.md).
- **Per-scope isolation cutover** (make pool-mode isolation structural): follow
  `ISOLATION-FIX-PLAN.md` phases 2–5, then bring up with
  `docker compose -f docker-compose.yml -f compose.scopes.yml up`.

---

## 7. Before real tenant data (safety checklist)

- [ ] Three **distinct** tokens set; `ALLOW_NO_AUTH` **unset**.
- [ ] n8n owner account enabled; UI reachable only on localhost (or behind IAP).
- [ ] `python scripts/conformance_test.py` passes.
- [ ] For **confidential / competing-tenant** scopes: they are `isolation:"silo"`
      **and** you've cut over to per-scope runners (§6). **Until then, pool-mode
      isolation is advisory (cwd only) — do not co-locate competing tenants**
      (threat-model **C1/C2**).
- [ ] Approval flows use real approver identities (threat-model **M5** — the
      resume link is still a bearer capability; authenticate the ingress).
- [ ] Read `THREAT-MODEL.md` for the current OPEN items (H2 rehydrate oracle, M1
      vault-at-rest, M8 secrets-in-env) and decide your residual-risk posture.

---

## 8. Troubleshooting (the known unknowns)

**8.1 In-container CLI login won't complete.** The OAuth localhost callback can't
reach your host browser. Use the CLI's code/paste flow, or open the printed URL on
your host and finish there. Alternatively copy an existing token file into the
volume (`~/.claude/.credentials.json` on Linux/WSL); on native Windows/macOS the
token may be in the OS keychain, so in-container login is the reliable path.

**8.2 Gemini runner fails / wrong flags.** The Gemini CLI's exact flags (`-p`,
`--yolo`, `--output-format json`, `-m`) and even the binary name may differ on
your machine (the free-tier CLI was migrating to "Antigravity"). Confirm, then set
`AGENT_BIN` / `PROMPT_FLAG` / `GEMINI_EXTRA_ARGS` in `.env` — no code change.

**8.3 A service exits immediately.** It's fail-closed: its token isn't set in
`.env` (or `ALLOW_NO_AUTH=1` for dev). Check `docker compose logs <service>`.

**8.4 Firewall blocks the runner.** `init-firewall.sh` is default-deny; add any
domain your agent needs to its `ALLOWED_DOMAINS` and rebuild. If it fails to apply
(missing `NET_ADMIN`), the container aborts by design (`FIREWALL=off` disables it
for local debugging only).

**8.5 A workflow import errors on a node.** n8n node `typeVersion`s drift across
versions; re-pick the node in the UI (the scaffolds' logic is unaffected). Validate
built workflows with the runner's `n8n` MCP tool (`validate_workflow`).

---

## 9. Running on Docker Engine in WSL2 (no Docker Desktop)

If Docker Desktop is unavailable or broken (e.g. the 4.71–4.82 "Inference manager"
startup crash on Windows), run the stack on **native Docker Engine inside WSL2** —
it's fully supported and bypasses Docker Desktop entirely.

```bash
# one-time, as root inside your WSL distro (wsl -u root):
apt-get update && apt-get install -y docker.io docker-compose-v2
service docker start                       # start the daemon (see reboot note below)

# run the stack (under WSL the repo lives at /mnt/<drive>/<path>):
cd /mnt/<drive>/<path>/ai-control-plane
docker compose up -d
```

### 9.1 Reaching the UIs from the Windows browser

Native WSL docker doesn't relay the base file's `127.0.0.1`-only binds to Windows.
Two ways to fix it — **mirrored networking is the durable one**:

**Preferred — mirrored networking + two narrow firewall rules.** Create
`%USERPROFILE%\.wslconfig`:
```ini
[wsl2]
networkingMode=mirrored
[experimental]
hostAddressLoopback=true
```
then `wsl --shutdown`. WSL now shares the Windows interfaces, so Windows
`localhost` reaches WSL's `127.0.0.1` — the UI ports stay **loopback-only** and
`docker-compose.wsl.yml` is not needed.

Mirrored mode gates host→WSL traffic behind the **Hyper-V firewall**, which
defaults to `DefaultInboundAction: Block` (check with
`Get-NetFirewallHyperVVMSetting -PolicyStore ActiveStore`). Open just the two UI
ports, in an **Administrator** PowerShell:
```powershell
$vm = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'   # the WSL VM creator id
New-NetFirewallHyperVRule -Name "WSL-acp-n8n" -DisplayName "WSL control-plane n8n (5678)" `
  -Direction Inbound -VMCreatorId $vm -Protocol TCP -LocalPorts 5678 -Action Allow
```
This leaves everything else in WSL blocked inbound. Do **not** flip
`DefaultInboundAction` to `Allow` — that opens every port in the VM to the host.

**Fallback — NAT + overlay.** No firewall change: skip `.wslconfig` and run
`docker compose -f docker-compose.yml -f docker-compose.wsl.yml up -d`, which
republishes the two UI ports on all interfaces inside the (NAT'd, non-LAN-exposed)
VM. Simpler, but the forwarding is flaky in practice.

Diagnosing: `curl` the port *inside* WSL first. A 200 there but a **timeout** from
Windows means the firewall is dropping it; **connection refused** means the
service itself isn't up yet (n8n takes ~10s to boot).
- **After a reboot** the WSL docker daemon does **not** auto-start (unlike Docker
  Desktop). Run `service docker start` (or enable systemd in `/etc/wsl.conf` with
  `[boot]\nsystemd=true`, then `systemctl enable --now docker`), then `up -d` again.
- **Performance:** bind-mounts off `/mnt/<drive>` are slower than the native WSL
  filesystem. For heavier use, clone the repo *inside* WSL (e.g. `~/ai-control-plane`).
- The runner firewalls (iptables + `NET_ADMIN`) work under the WSL2 kernel; the
  deterministic `ping → pong` smoke test in §2.6 confirms the router live without
  any CLI login.
