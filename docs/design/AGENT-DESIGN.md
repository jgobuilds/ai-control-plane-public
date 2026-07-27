# Agent design — scope, split, model, handoff

How to decide **how many agents**, **what each one owns**, **which model runs it**,
and **what crosses the boundary between them**.

Companion docs: [`AGENT-WORKSPACE.md`](AGENT-WORKSPACE.md) is the folder/context
layout this sits on; [`NAMING.md`](NAMING.md) names the personas;
[`context/model-policy.json`](../../context/model-policy.json) is the machine-readable
model/effort map that [`scripts/model_policy_check.py`](../../scripts/model_policy_check.py)
keeps honest.

> **This page reports a live disagreement rather than resolving it.** Two credible
> vendors published opposite defaults in June 2025, and one of them materially
> revised its position in April 2026. §1 gives both, plus the boundary they
> actually agree on — which is the part you can build on.

---

## Design intake — the six questions, in order

Before implementation, answer these six in sequence. Each decision constrains the
next: the goal bounds the resources, the resources bound the context, the context
shapes the reasoning, and so on. The mnemonic is the 5Ws + 1H framing (a
practitioner lens — memorable ordering, not a validated standard); each question
maps to where this framework already answers it in depth.

| Ask | The question | Answered in |
|---|---|---|
| **WHY** — goal | Objective, constraints, and how it knows it's done | Persona contract (§3), termination (§6) |
| **WHERE** — resources | What it can access, through what interface, within what trust boundary, what it remembers | `CONTEXT-ARCHITECTURE.md`, `AGENT-WORKSPACE.md` §3 |
| **WHAT** — context | Which of those resources reach the model, at what budget | `AGENT-WORKSPACE.md` §4 (budgets, progressive disclosure) |
| **HOW** — reasoning | Planning and reasoning strategy; which model/effort | §2 (patterns) + §2a (reasoning strategy) + §4 (model) |
| **WHEN** — loop | Trigger, retry, escalation, stop condition | §6, the circuit breaker (§7), persona template |
| **WHO** — team | One agent or several; humans in the loop | §1 (split), §5 (handoffs), operating mode |

Use it as a checklist, not a rewrite: the sections below carry the actual
guidance, grounded in failure data (§6) and a real vendor disagreement (§1) that
the intake framing does not.

---

## 1. One agent, or several?

### The disagreement, honestly

**Anthropic** built an orchestrator-worker research system (lead plans, 3–5
subagents search in parallel, lead synthesizes) and measured it **90.2% better
than a single-agent baseline** on their internal research eval — on a task
*designed* for parallel decomposition ("find all board members of S&P 500 IT
companies").

The same post contains the caveat that matters most: **token usage alone explains
~80% of the performance variance** on BrowseComp. Much of the multi-agent win is
brute-force parallel token spend, not emergent coordination. Their systems use
**~15× the tokens of a chat turn** (a plain single agent already uses ~4×).

**Cognition** published *Don't Build Multi-Agents* the same month, arguing
multi-agent collaboration "only results in fragile systems," on two principles:

1. *Share context — and share full agent **traces**, not just individual messages.*
2. *Actions carry implicit decisions, and conflicting decisions carry bad results.*

Their example: two subagents build a game in parallel; one produces a Mario-style
background, the other an incompatible bird sprite. Neither was wrong; neither
could see the other's implicit choice.

**Then Cognition moved.** In April 2026 they published *Multi-Agents: What's
Actually Working*, narrowing to a rule rather than a prohibition:

> Multiple agents may contribute **intelligence**; **writes stay single-threaded.**

Their shipped patterns: a clean-context **reviewer** (they report ~2 bugs/PR
caught, 58% rated severe), a **capability router** sending hard calls to a
stronger model, and **manager-child** delegation where only the coordinator writes.

### The boundary both sides agree on

| Split it | Keep it one agent |
|---|---|
| Read-heavy exploration | Write-heavy construction |
| Independently verifiable outputs | Outputs that must cohere with each other |
| Genuinely parallel subtasks | Sequential phases of one piece of work |
| Context cleanly separable | Work needs the accumulated implicit decisions |

**The single most useful rule, from Anthropic's own guidance:**

> **Decompose by *context boundaries*, not by problem type.**

Feature-writing, test-writing, and review of the *same change* share context —
that is **one** agent, not three. Splitting by job title is the classic mistake:
it looks like an org chart and buys nothing but coordination overhead.

### Split checklist — all must hold

Every box, or don't split:

- [ ] **Parallel** — subtasks are genuinely independent, not sequential phases.
- [ ] **Single writer** — exactly one agent mutates any given artifact. Others read or advise.
- [ ] **Separable context** — each subtask's context is a clean slice, not "everything so far."
- [ ] **Coherence not required** — outputs don't have to agree on style or architecture.
- [ ] **Cheaply verifiable** — you can check each output without replaying the producer's history.
- [ ] **Worth 3–10×** — the task justifies the token multiplier (vs. single-agent, apples to apples).

Three legitimate reasons to split, per Anthropic: **context protection**
(keeping irrelevant context out of the main reasoning), **parallelization**, and
**specialization** (tool confusion sets in past roughly 20 tools).

> ⚠️ **Two different multipliers, often conflated.** ~15× is *multi-agent vs. a
> chat turn*. 3–10× is *multi-agent vs. single-agent on equivalent tasks*. Use
> the second when justifying a split; the first overstates your case.

## 2. Escalate through patterns before reaching for agents

Most "we need agents" problems are workflow problems. In rising order of cost and
failure surface — stop at the first that works:

| Pattern | Fits | Failure mode |
|---|---|---|
| **Single call** | Classify, extract, summarize, one-shot generate | — |
| **Prompt chaining** | Fixed, decomposable sequence (outline → draft) | Latency with no accuracy gain when steps aren't really sequential |
| **Routing** | Distinct categories handled better separately | Breaks when classification is unreliable or categories overlap |
| **Parallelization — sectioning** | Independent subtasks concurrently (guardrail alongside generation) | Pure added cost on single-focus tasks |
| **Parallelization — voting** | Same task N times for confidence (independent vuln reviews) | Wasted spend when one take suffices |
| **Orchestrator-workers** | Decomposition can't be predicted in advance | The highest-token pattern; where most multi-agent failures live |
| **Evaluator-optimizer** | Clear eval criteria *and* iteration demonstrably helps | Wasteful with no clear criteria or when one pass suffices |
| **Autonomous agent** | Open-ended, step count unpredictable | Compounding errors; needs ground-truth feedback each step and a sandbox |

Anthropic's framing: **add complexity only when it demonstrably improves
outcomes.** A workflow is orchestrated by *code paths you wrote*; an agent
directs *itself*. Prefer the former — it is debuggable.

### 2a. Reasoning strategy — the loop inside a single agent

The patterns above are *orchestration* (how many agents, how they connect). This
is the level below: how one agent reasons through its own task. The deciding
question is **whether an early observation should change the plan.**

| Strategy | Fits | Failure mode |
|---|---|---|
| **Direct** (one call, no loop) | Bounded task fully specifiable up front | — |
| **Plan-then-execute** (ReWOO-style) | Steps are predictable; the plan won't change based on intermediate results. Fewer model calls — the plan is made once | Brittle when an early result *should* redirect the work; it executes the stale plan anyway |
| **ReAct** (reason → act → observe, interleaved) | Results genuinely shape the next step (search, tool-heavy exploration) | More calls, and the loop can spin — needs the §6 termination guard |
| **Reflection / Reflexion** (draft → critique → revise) | A clear quality bar exists and iteration measurably helps. This is the single-agent form of evaluator-optimizer (§2) | Pure cost when there's no criterion or one pass suffices; a self-critique with no fresh context is worth less than a clean-context reviewer |

Two rules that matter more than the labels:

- **Plan-then-execute vs ReAct is decided by adaptivity, not preference.** If the
  plan is fixed regardless of what comes back, plan-then-execute is cheaper. If
  the work must react to what it finds, ReAct — and pay for the extra calls.
- **Reflection is only as good as the critic's independence.** An agent grading
  its own output shares its blind spots; MAST (§6) found objective verification
  the highest-yield fix precisely when the verifier had a *fresh* context. Prefer
  a separate reviewer (the §5 clean-context pattern) over self-reflection when the
  stakes justify it.

Effort interacts with this: higher `effort` (§4) buys more internal reasoning per
call, which can substitute for extra ReAct iterations — raise effort before adding
loop steps.

## 3. The persona contract

Every agent gets four things written down. Template:
[`templates/agents/persona.example.json`](../../templates/agents/persona.example.json).
Naming follows [`NAMING.md`](NAMING.md) (`{Name} the {Role}`, alliterative).

| Field | Rule |
|---|---|
| **Scope** | What it owns **and explicitly does not**. If you can't describe it in one sentence, it's too broad. The "does not" half is what prevents overlap. |
| **Goal** | A *checkable* success criterion, not an aspiration. "Returns a ranked list with a severity per finding" — not "reviews code well." |
| **Tools** | Least privilege, expressed as a **narrow, time-bound task scope** rather than a permanent role grant. Watch the ~20-tool confusion threshold. |
| **Handoff** | Who it can hand to, what payload, and what it must *never* pass on. See §5. |

**Narrow beats broad** — widely reported in practice, and consistent with the
tool-confusion finding. Treat it as a strong heuristic, not a measured law: the
"narrow agents ship, broad agents stall" framing is industry commentary, not a
controlled study.

**Role proliferation is the failure on the other side.** If two personas need the
same context to do their jobs, they are one persona. Re-read §1's decompose-by-
context-boundaries rule before adding a name to the registry.

## 4. Which model, and at what effort

Full map with per-task justification:
[`context/model-policy.json`](../../context/model-policy.json). The shape of it:

| Work | Tier | Effort | Reasoning |
|---|---|---|---|
| classify, extract, transform, route | t1 (cheap) | `low` | Bounded output space; a bigger model buys nothing measurable |
| summarize, generate | t2 (`claude-sonnet-5`) | `medium`–`high` | Near-Opus quality on this work at a fraction of the cost |
| code-edit, debug | t2 | **`xhigh`** | Coding is the documented sweet spot for `xhigh` |
| plan, architect, test-design, review | t3 (`claude-fable-5`) | `high`–`xhigh` | Errors compound into everything downstream |

Four rules that matter more than the table:

1. **Raise effort before raising tier.** Effort is the smaller, cheaper step and
   is usually the actual fix for shallow output. It is the most under-used lever
   in the stack.
2. **Higher effort can *lower* total cost** on agentic work by cutting turn count.
   Cost-per-call is the wrong unit; cost-per-completed-task is the right one.
3. **Deterministic beats every model.** If the shape is stable, write the rule —
   free, instant, testable. That's what tier `t0` is for.
4. **Give the whole spec up front.** Long-horizon runs perform better *and*
   cheaper from one well-specified first turn than from intent revealed across
   turns. Match `max_tokens` to effort: at `xhigh`/`max`, budget ≥64000 or output
   truncates mid-thought.

## 5. Handoffs

A handoff is a **briefing written for the receiver**, not a transcript. The
receiving agent has no history and should not need any.

**Payload — required fields:**

| Field | Why |
|---|---|
| `objective` | What done looks like, in the receiver's terms |
| `acceptance` | How the receiver knows it succeeded — checkable |
| `context` | Only what the receiver needs. Not the sender's full trace |
| `owner` | Exactly one agent owns mutable state at any moment |
| `hops` | Incrementing counter with a hard ceiling — the loop guard |

**Two shapes, and they compose:** a **handoff** transfers control (the specialist
takes over); **agent-as-tool** keeps control (the manager calls a specialist for a
bounded subtask and continues). Prefer agent-as-tool — it preserves single
ownership by construction.

**Documented failure modes:**

- **Infinite delegation** — A→B→C→A because nobody owns the task. Mitigate with
  the hop ceiling *and* unambiguous escalate/reject/forward conditions per role.
- **Ambiguous ownership** — the load-bearing invariant is that at any moment
  exactly one agent owns the state and no one else may mutate it. This is
  Cognition's "conflicting implicit decisions" stated as a property you can check.
- **Lost context** — the receiver silently lacks something the sender assumed.
  Mitigate by making `acceptance` explicit rather than trusting shared understanding.

> **`A2A` status:** donated to the Linux Foundation's Agentic AI Foundation
> (alongside MCP), v1.0 with signed Agent Cards, 150+ supporting organizations.
> Its object model (Agent Card / Task / Message / Artifact) deliberately avoids
> sharing internal memory. **Field-level schema not verified here** — check the
> spec directly before encoding it, rather than trusting this summary.

## 6. What actually goes wrong

From MAST (*Why Do Multi-Agent LLM Systems Fail?*, NeurIPS 2025 Datasets &
Benchmarks) — 1,600+ annotated traces across 7 frameworks, 14 failure modes,
inter-annotator κ = 0.88. This is the best empirical grounding available, and it
should drive which controls you build:

| Category | Share | Worst offenders |
|---|---|---|
| **Design issues** | 44.2% | Step repetition **15.7%**, unaware of termination conditions **12.4%**, disobey task spec **11.8%** |
| **Inter-agent misalignment** | 32.4% | Reasoning-action mismatch **13.2%**, task derailment 7.4%, fail to ask for clarification 6.8% |
| **Task verification** | 23.5% | Incorrect verification **9.1%**, no/incomplete verification 8.2%, premature termination 6.2% |

**What the mitigations bought:** targeted workflow/prompt fixes for design issues
**+9.4%** success rate; adding high-level objective verification **+15.6%** — the
single highest-yield intervention measured.

**The uncomfortable finding:** inter-agent misalignment (a third of all failures)
is the category *least* fixable by prompting or protocol work. The paper
attributes it to needing deeper social reasoning from the models themselves. Do
not plan to prompt your way out of it — **design around it by not splitting**
(§1) rather than by coordinating harder.

Note the top failure — step repetition — and the third — unaware of termination —
are both *loop* failures. Termination conditions and hop ceilings are not
housekeeping; they are the two most common things that break.

## 7. How Brightworks implements this

- **Task type → tier/effort** is router policy, not prompt convention — one
  chokepoint, auditable per call. `verifyOnTiers` operationalizes MAST's
  highest-yield fix (objective verification) for exactly the tier where errors
  compound.
- **Risk × mode** already encodes "who leads, who validates" — the persona
  contract's `owner` field at the governance layer.
- **The kill switch and circuit breaker** are the loop guard for the two most
  common failure modes: a runaway loop trips `maxRequestsPerScope` inside a
  sliding window rather than burning budget until someone notices.
- **Structural isolation** (per-scope runners, mounts) is what makes "exactly one
  owner" enforceable rather than aspirational — an agent cannot mutate a scope it
  never had mounted.
- **`FANOUT_CONCURRENCY`** caps parallel workers: the token multiplier from §1
  with a ceiling on it.

## 8. Keeping this current

Model capability moves faster than standards do. Two mechanisms:

1. **Scheduled** — `context/model-policy.json` carries `reviewedOn` / `reviewBy`
   (90-day cadence). `scripts/model_policy_check.py` **fails** once the date
   passes, and warns ahead of retirements and pricing changes. A review reminder
   that can't fail is a review that doesn't happen.
2. **Triggered** — off-cycle review on: a new model release (a new mid-tier often
   absorbs work from the tier above), a price change (intro pricing expiring *is*
   a price rise), any deprecation notice, or an eval/drift regression
   ([`EVAL.md`](EVAL.md)) suggesting a tier is now under-powered.

**Re-test prompt scaffolding after any tier change.** Instructions written to work
around an older model's limitation become pure overhead on a newer one — and can
actively *reduce* output quality. Scaffolding is a dated artifact, not a permanent asset.

---

### Sources

[Anthropic — How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) ·
[Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents) ·
[Claude.com — When to use multi-agent systems](https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them) ·
[Cognition — Don't Build Multi-Agents](https://cognition.com/blog/dont-build-multi-agents) ·
[Cognition — Multi-Agents: What's Actually Working (2026-04)](https://cognition.com/blog/multi-agents-working) ·
[MAST — Why Do Multi-Agent LLM Systems Fail? (arXiv:2503.13657)](https://arxiv.org/abs/2503.13657) ·
[OpenAI Agents SDK — handoffs](https://openai.github.io/openai-agents-python/handoffs/) ·
[A2A protocol](https://a2a-protocol.org/latest/)

*Claims deliberately excluded as unsourced:* a widely-circulated "78% of
multi-agent systems never leave the lab" figure traces to no primary source;
"the debate has converged" is third-party commentary, not a joint vendor
position. Both were found and rejected during research rather than repeated.
