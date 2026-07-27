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
- **AG-3 (Supervised autonomy):** ✅ **first lane working** — the
  `incident-responder` workflow implements the full on-call maturity model:
  events (PagerDuty/Alertmanager/Slack/generic) → **skill-guided diagnosis** →
  **human-gated** fix in an isolated worktree with self-verify → **learning
  encoded back into skills** (Snowflake's exact loop; it took their KTLO toil
  from 30% to a 5% target). Second lane ✅: `dependency-bumps` — weekly scheduled
  fan-out (security bumps, minor bumps, majors report), self-verified branches,
  human merge. Its shape (Schedule → `/fanout` → summarize → Notify) is the
  template for every further lane (cleanup sweeps, lint debt, doc rot). Watch
  cost telemetry (agentsview) as lanes multiply.
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
| Cost telemetry / observability (AG-3) | **agentsview** — local session/cost dashboard | ✅ `agentsview` service, localhost:8090 |
| New capabilities on demand (AG-3/4) | **cli-printing-press** — generate CLI+MCP from any API | ✅ `capability-factory/` builder lane ([runbook](../runbooks/CAPABILITY-FACTORY.md)) |
| Fresh-context automated review (AG-2) | Claude Code **subagents** + `/code-review` (built in) | Available in the CLI; use from `/run` prompts |
| Parallel isolation (AG-2) | **git worktrees** (one per agent) | ✅ `POST /fanout` — worktree per agent, self-verify, human merge |
| Cost telemetry into an APM (AG-3) | Claude Code **OpenTelemetry** export (`CLAUDE_CODE_ENABLE_TELEMETRY`) | Optional add-on to agentsview |
| Token-proportional spend (AG-3) | **Model-splitting** (planner vs executor) | ✅ `model` + `reviewModel` on `/run` |
| Cost governance / lanes (AG-3/4) | **Cost-tiering router** — deterministic-first, cheapest-capable, `maxTier` ceiling, cross-provider verify | ✅ `router/` (`policy.json` = policy-as-code) |
| Second-vendor independent review | **gemini-runner** as a cross-provider verifier | ✅ router cross-verify |
| Standards as machine-readable context | `CLAUDE.md` + validated workflow scaffolds | ✅ |

## Measurement honesty

Agent count and human:agent ratios describe **shape, not success** — an unverified
swarm is risk, not leverage. Track **agentic leverage**: the share of shipped work
that is agent-produced *with the quality bar held* (change-failure rate flat or
falling). agentsview gives you the spend/volume side; your n8n execution success
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
