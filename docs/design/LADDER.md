# Climbing the Agentic Development Ladder with this stack

This framework is built to walk the **Agentic Development Ladder**
(AG-0 Gated → AG-4 AI-native), whose governing rules are: *the bottleneck
migrates at each stage*, *autonomy follows verification*, and *the harness owns
the safety surface*. This doc maps what's here to those stages and shows the next
rung.

## The four-layer stack → where it lives here

The ladder frames agentic work as four nested layers (`harness ⊃ loop ⊃ context ⊃
prompt`). Debug by identifying the failing layer first.

| Layer | Question | In this repo |
|---|---|---|
| **Harness** | What system does the loop run inside? | The firewalled `claude-runner` container + compose (sandbox, scoped creds, egress allowlist, budgets). *This is the safety surface — it's a harness decision, not a prompt.* |
| **Loop** | What cycle runs, what stops it? | `server.js` — agent + verifier, `--max-turns` budget, fresh-context review. n8n triggers decide *who starts the cycle*. |
| **Context** | What does the model see? | `workspace/CLAUDE.md` + the **n8n-MCP** real node schemas (so it isn't guessing). |
| **Prompt** | How is the message worded? | The `prompt`/`goal` fields on `/run`. |

> "We need better prompts" is usually a misdiagnosis of a context or harness
> problem. At AG-2+, harness investment beats model upgrades.

### The harness scales; the other three layers do not

Loop, context and prompt are the same whether one person runs this on a laptop
or a company runs it for fifty. **Only the harness changes**, and it changes
along one axis — how strong the wall between tenants has to be.

| Deployment | Harness | Isolation | When it stops being right |
|---|---|---|---|
| **Single user, one machine** | This compose stack on a workstation. One `claude-runner`, `cwd` scoping | Pool — the working directory is the boundary | The moment a second party's material arrives, or an obligation attaches to it |
| **Single user, siloed runners** | Same stack, one runner container per tenant, each with its own mount and egress allowlist | Silo — the container is the boundary | When the machine must be on for a schedule it does not control, or backups become somebody's job |
| **Team / always-on host** | One small server: compose under systemd, no public ingress, identity-aware proxy, managed secrets, backups ([ADR 0009](../decisions/0009-where-the-control-plane-runs.md) option B) | Silo, plus real secret management | When a second operator needs their own access, or a contract names a region |
| **Company, cloud provider** | The same services on managed infrastructure — see [`context/cloud-providers.json`](../../context/cloud-providers.json) for the per-capability provider seam and `scripts/cloud.py --egress` for what each choice opens | Silo per tenant, plus provider-level controls (VPC-SC, private endpoints, KMS, DLP) | — |

**The stack does not change shape when you move up this table.** The router is
still the only path to a runner; the ledger is still append-only; the scopes are
still the directory tree. What changes is who operates the wall — you, or a
provider you can point an auditor at.

Two things worth stating plainly, because both are usually discovered late:

- **A cloud does not give you isolation, it gives you the means to build it.**
  Putting the same pooled runner in AWS or Azure moves the risk, it does not
  reduce it. The isolation decision is the one in
  [`CONTEXT-ARCHITECTURE.md`](CONTEXT-ARCHITECTURE.md), and it is unchanged by
  where the container runs.
- **Adopting a provider is an explicit egress decision.** Runners are
  default-deny; every capability you turn on opens named domains. That is why
  the registry lists them per provider — so it is a decision, not a discovery.

### Licensing the agent CLIs — check this before scaling anything

The harness runs vendor CLIs, and **their terms, not this repo's, govern how many
agents you may run and on what plan.** Get this right before adding runners:

- **Interactive vs automated use are billed differently, and the line has
  already moved once.** Since **2026-06-15**, headless `claude -p` on a Claude
  subscription draws on a per-user monthly Agent SDK credit at standard API
  rates, not on plan usage limits — and stops at the ceiling unless usage
  credits are enabled. Scaling runners is therefore a spend question with a hard
  stop, not a rate-limit question. Do not plan around this as permanent either.
  The dated per-provider matrix — auth path, billing, whether unattended use is
  permitted, confidence, primary sources — is in
  [`PROVIDER-BILLING.md`](PROVIDER-BILLING.md).
- **A seat is a person, not a container.** Do not assume one subscription
  legitimises N parallel runners. If you need fleet width, an API key or an
  enterprise agreement is the honest path, and it is metered.
- **Free tiers usually carry different data terms.** A free key may permit the
  provider to retain or review submitted content, where a paid or enterprise tier
  under a DPA does not. For tenant material that distinction matters more than
  the price — see [ADR 0015](../decisions/0015-call-transcription.md).
- **Credential per tenant, not per fleet.** In silo mode each runner has its own
  auth volume; use it. One shared credential across tenants makes the audit trail
  unable to answer "who did this on whose behalf".
- **Terms change under you.** The pricing change above retired this project's
  original differentiator without anyone doing anything. Put the vendor's terms
  page in the research watch and date the finding.

## Loop types → how to run each here

| Loop type | Ladder stage | How |
|---|---|---|
| **Turn-based** (you trigger each cycle) | AG-1 | `POST /run` from a manual n8n trigger. |
| **Goal-based** (runs until a check passes) | AG-1→2 | `POST /run` with `verify:true` + `goal`. The verifier is the gate. |
| **Scheduled** (runs on a clock over a stream) | AG-3 | n8n **Schedule Trigger** → `/run` (bug triage, dependency bumps, cleanup). |
| **Proactive** (event-triggered, starts itself) | AG-3 scoped → AG-4 default | n8n **Webhook/DB/queue trigger** → `/run`. Scope narrowly first. |

## Where this stack sits, and the next rung

- **AG-1 (Assisted):** ✅ working today — one `/run` call, supervised.
- **AG-2 (Parallel):** ✅ **working today** — `POST /fanout` runs several agents
  concurrently, each isolated in **its own git worktree** on branch `agent/<id>`,
  each **self-verifying** (a shell check per task). The runner never merges or
  pushes; you review diffs/patches and merge by hand. Bounded concurrency
  (`FANOUT_CONCURRENCY`). See `n8n-workflows/parallel-fanout.example.json`.
- **AG-3 (Supervised autonomy):** ⚠️ **built, not yet exercised.** Both lanes below
  are loaded in n8n and **idle** — neither has carried real work, so by this
  ladder's own rule (score by behaviour, not by code that could run) this rung is
  not reached. Corrected 2026-09-24: this bullet read "first lane working", which
  the generated architecture page contradicted. The
  `incident-responder` workflow implements the full on-call maturity model:
  events (PagerDuty/Alertmanager/Slack/generic) → **skill-guided diagnosis** →
  **human-gated** fix in an isolated worktree with self-verify → **learning
  encoded back into skills** (Snowflake's exact loop; it took their KTLO toil
  from 30% to a 5% target). Second lane, also idle: `dependency-bumps` — weekly scheduled
  fan-out (security bumps, minor bumps, majors report), self-verified branches,
  human merge. Its shape (Schedule → `/fanout` → summarize → Notify) is the
  template for every further lane (cleanup sweeps, lint debt, doc rot). Watch
  cost telemetry (`scripts/gen_status.py`) as lanes multiply.
- **AG-4 (AI-native):** agents starting agents under **lanes** — automation lanes
  (closed-loop, per-workflow cost caps, exception monitoring) vs human-gated lanes
  (prod deploys, security-sensitive changes). The `builder` profile vs the locked
  `runner` is the first instance of this lane split.

## Loop-engineering discipline (enforced, not just documented)

1. **A loop is agent + verifier, minimum** → `verify:true` runs a *separate,
   clean-context* reviewer; a loop reviewing its own output inherits its own bias.
2. **Every loop gets a budget + stop condition** → `--max-turns` /
   `DEFAULT_MAX_TURNS`. Unbounded loops are billing incidents.
3. **Review with fresh context** → the verifier is a new process that sees only
   goal + output, never the executor's reasoning.
4. **Fix the harness, not the bug** → when review fails, update `CLAUDE.md`/checks,
   don't patch the single output. (Encoded as a rule in `CLAUDE.md`.)
5. **Loop quality mirrors codebase quality** → this is why the n8n-MCP schemas +
   validation gates come *before* scaling agents.

## Supplementary tooling (what supports each rung)

| Need (ladder) | Tool | Status here |
|---|---|---|
| Cost telemetry / observability (AG-3) | A local status + session view | ✅ `scripts/gen_status.py` → `status.html` (gates, containers, lanes, ledger, sessions) |
| New capabilities on demand (AG-3/4) | **cli-printing-press** — generate CLI+MCP from any API | ✅ `capability-factory/` builder lane ([runbook](../runbooks/CAPABILITY-FACTORY.md)) |
| Fresh-context automated review (AG-2) | Claude Code **subagents** + `/code-review` (built in) | Available in the CLI; use from `/run` prompts |
| Parallel isolation (AG-2) | **git worktrees** (one per agent) | ✅ `POST /fanout` — worktree per agent, self-verify, human merge |
| Cost telemetry into an APM (AG-3) | Claude Code **OpenTelemetry** export (`CLAUDE_CODE_ENABLE_TELEMETRY`) | Optional add-on to the status page; still unwiriew |
| Token-proportional spend (AG-3) | **Model-splitting** (planner vs executor) | ✅ `model` + `reviewModel` on `/run` |
| Cost governance / lanes (AG-3/4) | **Cost-tiering router** — deterministic-first, cheapest-capable, `maxTier` ceiling, cross-provider verify | ✅ `router/` (`policy.json` = policy-as-code) |
| Second-vendor independent review | **gemini-runner** as a cross-provider verifier | ✅ router cross-verify |
| Standards as machine-readable context | `CLAUDE.md` + validated workflow scaffolds | ✅ |

## Measurement honesty

Agent count and human:agent ratios describe **shape, not success** — an unverified
swarm is risk, not leverage. Track **agentic leverage**: the share of shipped work
that is agent-produced *with the quality bar held* (change-failure rate flat or
falling). `status.html` gives you the spend/volume side; your n8n execution success
rate + review pass rate give you the quality side. **Never gate on raw agent count.**

> **Field validation.** Snowflake ran exactly this play: they tracked *usage
> frequency* (97% weekly-active), explicitly **not** gameable metrics like lines
> of code or PR counts, then measured outcomes — release validation 15 days → 1
> day, test coverage 3.5×, a 3-person team hitting 40× on a query compiler. The
> "fence your robots" pattern (isolated git worktrees per agent) is precisely what
> `/fanout` implements. Same ladder, same bottlenecks.

## Score by behavior, not license

Buying capacity doesn't advance a rung — breaking the current bottleneck and
building the next guardrail does. Ten parallel `/run` calls are still AG-1 until
there's a trusted verification loop behind them. That loop is why `verify` exists.
