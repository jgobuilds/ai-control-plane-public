# Run on Docker Desktop on the developer desktop now, on one small always-on host when a second person depends on it — not Kubernetes

**Date:** 2026-07-25 · **Lens:** `ai-standards/references/cost-awareness.md`,
`change-safety.md`, `dependency-security.md`, `notification-taxonomy.md`

## Considered

Facts came from **probing the running system**, not memory — and one first-pass fact
was wrong, which is recorded here because it changed the recommendation.

What forced this decision: `wsl -l -v` reports the `Ubuntu` distro **Stopped**
whenever no session holds it open, and the distro takes `dockerd` and all seven
containers with it. Verified both directions — holding a session, distro PID 1 age
climbed 12s → 38s → 63s, n8n stayed up, and `localhost:5678` became reachable from
Windows; on release, the stack died. Mirrored networking in `.wslconfig` works and
was never the problem.

The consequence is larger than an unreachable UI: **n8n cannot run a cron when its
container only exists while a terminal is open, and it does not backfill missed
schedules.** Every lane marked "active" today would sit dead. This is why no
scheduled lane has ever fired on its own.

**Corrected fact.** A first check for `C:\Program Files\Docker\Docker` concluded
Docker Desktop was not installed. It is — **4.82.0**, a *per-user* install at
`%LOCALAPPDATA%\Programs\DockerDesktop`, missed because the probe only looked
machine-wide. Its engine is a **separate daemon**: `docker context ls` shows
`desktop-linux`, and that engine currently holds **zero containers and zero
volumes**. Our entire stack lives in Ubuntu's `dockerd`, so adopting Docker Desktop
is a **volume migration**, not a toggle.

| Option | Cost | Verdict |
|---|---|---|
| **A2. Desktop, on the already-installed Docker Desktop engine** | Licence class: **free for individuals; paid per-seat above Docker's company-size threshold** — a commercial input for the company case, already accepted here. Migration toil: one-time, moving 4 named volumes. | **Adopt now** — it is the supported lifecycle (starts at sign-in, keeps its own distro alive, honours `restart: unless-stopped`), so it removes the failure rather than working around it. |
| A1. Desktop + Scheduled Task at logon holding a WSL session open | Free; no install, no licence. Non-dollar: a keepalive process whose only job is to stop the platform dying — pure workaround, and one more thing to explain. | Reject as the primary — keep as the **fallback** if the migration hits trouble, since it needs no data movement. |
| A3. Desktop + Task at *boot* starting `dockerd` pre-login | Free; higher toil — WSL in a pre-login context is fragile and needs its own verification. | Reject — more moving parts for an availability gain a desktop cannot honour anyway. |
| **B. One small always-on Linux host, Compose + systemd** | Fixed, low monthly (small-VM class); trigger is **the first month it runs**, not usage. Non-dollar: patching, backups, building an access path. **No Docker Desktop licence** — plain Docker Engine on Linux. | **Adopt when triggered** (see Status) — the only option where "scheduled" is true. |
| C. Kubernetes (GKE Autopilot / EKS / AKS) | Fixed monthly **control-plane charge before any workload**, typically *more* than B's single VM, plus real operational skill. | Reject — a scaling answer, not an availability-cost answer. It does not remove always-on infrastructure; it adds an always-on control plane to manage our control plane. |
| D. Scale-to-zero serverless (Cloud Run / Fargate / ACI) | Metered per-request, near-zero at rest. Non-dollar: high — a scaled-to-zero service has no clock. | Reject as the whole answer — n8n is a *stateful scheduler*; scaling it to zero removes the thing we are trying to run. |
| E. Managed scheduler (Cloud Scheduler / EventBridge / GH Actions cron) waking an ephemeral stack | Scheduler: free-tier class. Compute: metered seconds. Non-dollar: **high** — cold-starting seven containers per run, and `audit/` + the n8n DB need external durable state. | Defer — the honest version of D, and the right shape *if* B's cost ever stops being trivial. Unpriced for our image sizes; needs a costed spike. |

## Chose

1. **Now — A2.** Move the stack to the Docker Desktop engine and enable *Start
   Docker Desktop when you sign in*. Containers already carry
   `restart: unless-stopped`, so the lifecycle becomes the platform's job.
2. **Migrate the named volumes, do not recreate them.** `n8n_data` holds the
   workflow database **and n8n's encryption key** — recreating it silently
   invalidates stored credentials. `claude-config` and `gemini-config` hold the
   runners' logins. Same backup/restore procedure already executed successfully
   during the folder rename; bind mounts on `D:` need no migration.
3. **Align lane schedules to hours the desktop is realistically on** (business
   hours, not 03:15). n8n does not backfill, so a cron aimed at a sleeping machine
   never runs. This moved the retention lane to 06:00.
4. **Later — B**, one small always-on host: Compose under systemd, **no public
   ingress** (identity-aware proxy — Tailscale / Cloudflare Access / GCP IAP, see
   `docs/runbooks/GSUITE-GCP.md`), managed secrets instead of `.env`, backups of
   `audit/` and the n8n database.
5. **Not Kubernetes**, at this size or the next one.
6. **The host migration is also the identity migration.** The runners authenticate
   today with **personal subscription tokens**; a shared service must use
   **organisation API credentials**. A scheduled job may act *for* a person and must
   never act *as* one — the human stays `requester`/`owner` in the ledger (the router
   already carries both, and `rbac.js` enforces segregation of duties against them),
   while the workload authenticates as itself, with permissions provisioned as a
   subset of its owner's and revoked by `offboard_scope.py`. These ship together;
   splitting them risks the host landing with personal credentials on it.

## Because

**Kubernetes is the option most likely to be chosen for the wrong reason.** It
answers "how do I run many workloads elastically", which is not our problem. Ours is
"something must hold a clock". Every managed k8s offering bills a control plane
whether or not a pod runs, so it is strictly *more* always-on infrastructure than the
single VM it would replace, plus a surface to secure, patch and monitor.

The same reasoning kills scale-to-zero as a *whole* answer, and it is worth being
precise about why: the workers can be ephemeral, but **the scheduler cannot**. Option
E is the honest version of that split — an always-on managed clock plus burst compute
— deferred rather than rejected because it is real engineering spent to avoid a small
fixed bill. The work costs more than the bill. That is the trade, stated now rather
than discovered later.

A2 beats A1 on a principle worth naming: **a keepalive process exists only to stop
the platform from dying, and solves nothing else.** Docker Desktop already manages
the distro lifecycle we were about to hand-roll. Choosing the workaround when the
supported mechanism is installed and running would be building what we already have.

What A2 gives up: a **licence dependency** in the base platform. Free for an
individual, chargeable above Docker's company-size threshold — which is a live input
for the company question, not a footnote. Option B avoids it entirely, because plain
Docker Engine on Linux carries no such terms. It also gives up availability whenever
the desktop is off or signed out; A2 makes the schedule *possible*, not *reliable*.

What B gives up: money, patching, and an access path to build. Those are the costs of
the property we want — that the system keeps working when the person who built it
closes their laptop.

## Status

**A2: implemented and verified 2026-07-25.** Migration performed backup-first: all
four named volumes archived to `D:\code\_volume-backups\2026-07-25\`, with the
`n8n-data` archive re-taken **cold** after stopping the stack — the first copy was
hot and carried `database.sqlite-wal`, which is a copy that can be missing committed
transactions. Restored into the desktop engine, then verified rather than assumed:

- `database.sqlite` (2.3 MB) and `config` present, ownership `1000:1000` preserved,
  and `config` still carries `encryptionKey` — so stored credentials survive.
- All **13 workflows** and all **7 active states** present after the move.
- `localhost:5678` and `localhost:8090` reachable **from Windows**.
- End-to-end: `research-watch` ran to `status: success`, routed to the agent
  (t3/claude-fable-5, new ledger entry at 23:11:56), and Notify returned Slack's
  `"ok"`. The runner's login survived the `claude-config` volume move.
- Audit chain re-verified after the move: **15 records, hash chain intact.**

**Rollback is live, not theoretical:** the Ubuntu engine still holds all four
volumes and its containers are merely `Exited (0)`. Nothing was deleted.

**One step outstanding and it is the one that matters:** Docker Desktop's
`AutoStart` is **False** (`%APPDATA%\Docker\settings-store.json`). Until *Settings →
General → "Start Docker Desktop when you sign in"* is enabled, this migration fixes
reachability but **not** the scheduling problem it was done for.

**Schedule realignment: applied 2026-07-25.** Retention moved 03:15 -> 06:00, and
Docker Desktop's `AutoStart` is now **True** (verified in `settings-store.json`,
not taken on trust), so the stack comes back at sign-in.

**B: decided-not-built.** Costs are stated as classes and triggers; no billing account
exists, which is a decision input rather than a footnote.

**Revisit when** any one of these is true — each concrete, none a vibe:

- **A second person needs to approve something.** The approval gate is theatre when
  only one machine can render it. Strongest trigger.
- **The ledger is asked for as evidence** by a client, auditor or contract. Hash-
  chained tamper evidence on an unbacked-up laptop disk is one drive failure from
  worthless.
- **A lane must run while the desktop is off** — anything overnight or on a day off.
- **A second machine** joins, at which point "which laptop is the source of truth"
  has no good answer.
- **Docker Desktop's licence terms start applying** to the entity running this.
- Or by **2026-10-25**, whichever comes first, so this cannot become permanent by
  nobody re-reading it.
