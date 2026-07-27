# External assessment — Paperclip + *AI Agents in Action* (2e) vs. this stack

**Date:** 2026-07-25 · **Assessed against:** `ai-control-plane` (this repo),
[`ai-standards/`](../../../ai-standards), the private `<org>-instance/` overlay
**Lenses applied:** `build-vs-adopt`, `change-safety`, `dependency-security`,
`cost-awareness`, `agent-context-architecture`

Two external reference points, read in full, scored for what they add and what
they expose:

| Source | What it is | Verified facts |
|---|---|---|
| [paperclipai/paperclip](https://github.com/paperclipai/paperclip) | "The open-source app everyone uses to manage agents at work" — Node + React + Postgres org/task layer over BYO agents | MIT · 74.7k ★ · 13.9k forks · created **2026-03-02** · 4,945 open issues · **2,919 open PRs** · 3,264 commits · default branch `master` (verified via `gh api`, 2026-07-25) |
| Micheal Lanham, *AI Agents in Action, 2nd ed.* (Manning, 2026, ISBN 9781633433120) | 11-chapter agent-engineering text: five functional layers, MCP, multi-agent, reasoning, RAG/memory, eval, deployment/security, agentic loop, cognitive architecture | Sponsored ePDF, 391 pp. Ch. 8.4 is the security/governance chapter; ch. 10 is the cognitive-architecture chapter |

---

## 0. The framing that matters

These three things sit in **different planes**, and most of the apparent overlap
dissolves once that is stated:

```
   Lanham (the book)     →  what happens INSIDE an agent and between agents
   Paperclip             →  what WORK the agents are assigned and who owns it
   ai-control-plane      →  what an agent is ALLOWED to do, and the proof it did
```

`docs/plans/MARKET-ANALYSIS.md` splits the market into four categories —
governance platforms, gateways, orchestration, agent security — and claims this
stack spans all four. That claim survives contact with Paperclip, but the
analysis has a **missing fifth category: agent work-management / org modeling.**
Paperclip is the category leader in it by a wide margin, and nothing here
occupies it. That is a genuine hole in our own market read, not just a feature
gap. **MARKET-ANALYSIS.md should be amended.**

---

## 1. Paperclip — what it exposes

### 1.1 Real gaps it fills that we do not

| # | Gap | Evidence here | What Paperclip does |
|---|---|---|---|
| **P1** | **No unit of work.** We have *lanes*; we have no *task* with identity, assignee, state, parent, or thread. `grep -ri ticket` over this repo returns **1 hit**. | `n8n-workflows/*` are triggers, not work items | Issues carrying company/project/goal/parent links, comments, attachments, work products, inbox state |
| **P2** | **No execution lease.** Two lanes firing on the same target duplicate work silently. `ai-standards/references/concurrency-and-branching.md` names "leases for singular state" as a *rule* — nothing in this repo implements one. | — | **Atomic checkout with execution locks** — checkout and budget debit in one transaction |
| **P3** | **No resumable agent state.** `/run` is one-shot; every invocation restarts cold. Long-horizon work is not expressible. | `claude-runner/server.js` | Heartbeats resume the same task context across wakeups; DB-backed wakeup queue with coalescing |
| **P4** | **Approval ingress is an unauthenticated bearer URL** — our own threat-model **M5**, still open. | `n8n-workflows/approval-gate.subworkflow.json` builds `${resumeUrl}?approved=true` | Board approvals bound to authenticated users, run JWTs, multi-user memberships, invite flows |
| **P5** | **Cost attribution is per-scope only.** The breaker refuses *new* work; it cannot cancel *queued* work, and cannot answer "what did this project cost". | `router/policy.json → limits` | Cost tracked by company/agent/project/goal/issue/provider/model; warn thresholds; overspend **pauses the agent and cancels its queue** |
| **P6** | **No orphaned-run recovery.** The n8n Global Error Handler catches failures; nothing reaps a run whose runner died mid-flight. | — | Automatic orphaned-run recovery, self-healing runs |
| **P7** | **Single host, single sandbox shape.** The C1 fix (`docs/plans/ISOLATION-FIX-PLAN.md`) is being designed from scratch. | `compose.scopes.yml` generator | Shipped adapters for e2b, Cloudflare, Daytona, Modal, Novita, self-hosted k8s — **adoptable prior art for C1**, per `build-vs-adopt` |
| **P8** | **Tool allowlisting is delegated to the vendor CLI.** We pass `--allowedTools`; we do not broker the calls, so we cannot log or deny at call time. This is the weak half of **H3**. | `docs/security/GUARDRAILS.md` | Shipped **MCP Tool Gateway** — governed tool access as a broker |
| **P9** | **No scope export/import.** `scripts/offboard_scope.py` deletes; nothing packages. | — | Company export/import with secret scrubbing and collision handling |

### 1.2 Where we are ahead — and it is not close

1. **Enforcement is structural and regression-tested.** Paperclip's governance is
   application logic over Postgres. Ours is: hash-chained append-only ledger
   (`scripts/verify_audit.py`), default-deny egress firewall that **aborts the
   container on failed apply**, per-service tokens with `timingSafeEqual`, and
   `scripts/conformance_test.py` failing CI when an invariant breaks. Paperclip
   claims an "immutable audit log"; nothing in the README describes tamper
   evidence, and there is no published threat model.
2. **We publish our own criticals.** `docs/security/THREAT-MODEL.md` ranks C1/C2
   as open and states plainly that the marquee isolation guarantee is advisory in
   pool mode. Paperclip publishes no equivalent. An honest open finding is worth
   more than an unexamined surface.
3. **Risk × mode → proportional controls**, mapped to OWASP Agentic / NIST AI RMF
   / EU AI Act with honest full/partial/none coverage
   (`compliance/framework-map.yaml`). Paperclip has approvals and budgets; it has
   no risk model and no regulatory mapping.
4. **PII never reaches the model.** Scrubber tokenizes on ingest, vault mounts
   only into the scrubber, `pii:block` refuses raw PII in prompts. Paperclip's
   nearest statement is "sensitive values stay out of prompts unless a scoped run
   needs them" — a secrets story, not a PII story.
5. **Deterministic tier-0 + subscription billing.** Paperclip *measures* metered
   spend well. We *avoid* it. Orthogonal, and ours is the larger lever.

### 1.3 Two disqualifiers for adopting it as the control plane

- **Telemetry is enabled by default** (`PAPERCLIP_TELEMETRY_DISABLED=1` /
  `DO_NOT_TRACK=1` to opt out). Against the positioning in MARKET-ANALYSIS row 5
  — *"no third party ever sees the data"* — and against
  `ai-standards/references/pii-controls.md`, default-on egress to a vendor is a
  posture change, not a setting. It is disableable, so this is a **configuration
  gate, not a veto** — but it must be closed before any client-adjacent use.
- **Maintenance surface.** 2,919 open PRs and 4,945 open issues on a five-month-old
  repo. Under `dependency-security`'s **privileged-path principle**, the bar
  scales with position, and a control plane is the most privileged position there
  is. Popularity (74.7k ★) is explicitly *not* the metric that clears that bar.

**Verdict on Paperclip: adopt the ideas, not the dependency.** P1/P2/P3 are the
three that change what this stack can do; P7 and P8 are where its code is worth
reading before we write ours.

### 1.4 One philosophical warning

Paperclip's pitch is "autonomous AI companies" and org charts — *"If you have
twenty [agents] — you definitely do [need this]."* `docs/design/LADDER.md` says
the opposite in as many words: **agent count describes shape, not success; never
gate on raw agent count.** Adopting Paperclip's work model without its framing is
correct. Adopting its framing would quietly re-introduce the vanity metric the
ladder exists to reject.

---

## 2. Lanham, *AI Agents in Action* 2e — what it exposes

### 2.1 Where the book validates what is already built

The book's §8.4 baseline reads almost as a description of this stack — which is a
result, not a coincidence:

| Book prescription (§8.4) | Here |
|---|---|
| Threat-model by *surface → asset*, not checklist ("going through the list as a checklist gives you the appearance of security") | `THREAT-MODEL.md` — trust boundaries outermost→innermost, findings ranked by distance from design intent |
| "Policies are best enforced **outside** the agents themselves… Putting policy in the prompt is fragile and unauditable" | `router/policy.json` — policy-as-code at a chokepoint, with which version was active recorded per decision |
| Policy registry: machine-enforceable rules, overrides logged with reason codes | `policy.json` + `decision.blocked` + reason in the ledger |
| Deny egress by default; outbound allowlist | `init-firewall.sh`, fail-closed |
| Rate limiting per tenant to catch runaway cost and compromised credentials | `policy.json → limits` sliding-window breaker (rate **and** notional spend) |
| Audit logs add tamper-resistance and longer retention over observability logs | hash-chained ledger + `RETENTION.md` + signed deletion certificates |
| HITL trigger on **action stakes** (irreversibility, financial impact, external visibility), not on every action | `RISK-MODEL.md` — access dimension is literally advise→read→write→ingest→apply→send→execute |
| Durable async state + explicit timeout/escalation for long approvals | n8n Wait node, 24h `limitWaitTime`, **default-deny on timeout** |
| Pin exact model and tool versions for reproducibility | `context/model-policy.json` + `scripts/model_policy_check.py` — **ahead of the book**: our checker *fails* when a review date passes or a routed model deprecates |

### 2.2 Gaps the book exposes — specific and cheap

**B1 — There is no instruction hierarchy in the runner's system prompt.**
`grep -in "untrusted|injection|instruction hierarchy|never follow"
workspace/CLAUDE.md` returns **nothing**. Our entire H3 answer is heuristic
`inputScan`/`outputScan` at the router plus per-scope `allowedTools`. The book's
most important architectural claim is the one we have not implemented:

> "Treating tool outputs as untrusted data rather than as instructions is the
> most important architectural defense: the agent should reason about retrieved
> content, not execute instructions found in it."

Listing 8.8 is a directly adoptable artifact — priority order (system prompt >
tool contracts > developer > user > retrieved content), never-execute-content,
allowlists-over-denylists, post-checks-before-acting. This is a **prompt-layer
control we deliberately do not have**, and the ingest lanes (`drive-sync`,
`incident-responder`) are exactly the indirect-injection surface it addresses.
Cost: one section in `workspace/CLAUDE.md`. It does not replace C1; it is the
cheap layer that should have existed while C1 is deferred.

**B2 — M7 (no CPU/mem/pids limits) is under-prioritized.** Verified:
`docker-compose.yml` has no `deploy.resources`, `mem_limit`, `cpus`, or
`ulimits`. The book lists resource limits as one of *four* baseline tool-safety
controls alongside sandbox, filesystem scoping, and egress — we have three of
four. It is currently ranked Medium; it is a one-line-per-service fix and the
only baseline control missing outright. **Promote it.**

**B3 — The approval payload is free text.** Verified: `approval-gate` takes
`{title, message, source}` and emits two links. The book's second HITL design
decision is the one we fail:

> "the reviewer needs enough context… because bare approve/reject prompts produce
> rubber-stamp approvals that defeat the purpose."

The gate is structurally sound (durable wait, default-deny timeout, router-side
RBAC + SoD) and then hands the human a string. It should carry a **typed
contract** — action, scope, risk level and which dimensions fired, requester,
what data is touched, and a diff/preview — all of which the router already
computes into `decision`. This is plumbing, not new capability.

**B4 — No confidence signal anywhere.** The single most transferable idea in the
book is ch. 10's **metacognitive monitoring**, and its three components map onto
holes we have:

| Book (ch. 10.3) | Gap here |
|---|---|
| **Confidence-gated execution** | `verify` returns a binary verdict. `EVAL.md` admits the leverage metric is a proxy because "verdict is a verifier pass, not proof of correctness." A graded confidence in `decision` would sharpen that metric materially |
| **Stagnation detection** and strategy pivot | Loops stop on `--max-turns` only. A loop burning its full budget going nowhere is indistinguishable from one that succeeded on the last turn |
| **Knowledge-boundary awareness** ("I don't know" as an off-ramp) | No prescribed refusal path in `workspace/CLAUDE.md` or the persona contract in `AGENT-DESIGN.md` §3 |

The circuit breaker currently trips on rate and notional spend. A **quality-based
trip** — stagnation, or a confidence-weighted verdict trend — is the natural next
dimension and fits `killswitch.js` without redesign.

**B5 — Grounding agents and rubric critics (ch. 7.3).** Our verifier is a generic
fresh-context reviewer with a goal. The book's rubric-critic pattern (explicit
criteria, scored) would make `verify_pass_pct` less noisy and give
`scripts/eval_run.py`'s LLM-as-judge mode a defined rubric instead of an implicit
one.

**B6 — Idempotency.** `grep -ri idempot` → **0 hits**. The book flags it as a
prerequisite for retries, replay, and caching. Our lanes retry; re-running an
`apply` is not idempotent. Small, real, and it interacts with P2 above.

**B7 — Span-level tracing.** The book (§8.3, ch. 7.4 Phoenix) and Paperclip
(opt-in OTel) **converge** on trace UI → gateway → agent → tools → model.
`LADDER.md` already lists Claude Code's OTel export as an optional add-on. Two
independent sources landing on the same item is a reason to move it out of
"optional."

**B8 — Layered caching.** Book: prompt/response/embedding caching cuts 30–80% on
hot paths. Our tier-0 deterministic rules are a coarser version of the same idea.
Worth noting honestly per `cost-awareness`: **under subscription billing the
return is rate-limit headroom and latency, not dollars** — the same distinction
`AIOPS.md` already makes about notional cost. That framing is ours and it is
better than the book's.

**B9 — Content safety / moderation.** `grep -ri "content safety|moderation"` →
**0 hits**. Listed by the book as a first-class policy category. For internal
consulting work this is genuinely low priority — record it as a *scoped-out*
decision rather than an omission.

**B10 — Identity is scope-shaped, not principal-shaped.** The book: "agents
acting on behalf of users have the same limited access as the users themselves,"
with user authorization passed through to every tool and service call. Our RBAC
is policy-file `identityGroup` strings with no identity provider behind them —
which is the *other* half of M5. Paperclip solved this with run JWTs. Both
external sources point at the same weakness.

### 2.3 Where we are ahead of the book

- **Model policy with expiry.** The book says pin versions. We fail CI when the
  pin's review date passes. That is a stronger control than the text describes.
- **Deliberate, dated deferral of the memory layer.** The book treats
  RAG/memory/knowledge-graph as things you *should* build (ch. 6, ch. 10.2.7).
  `docs/decisions/0008-context-and-knowledge-at-scale.md` weighs Graphiti /
  Cognee / Mem0 / GraphRAG with cost and licence, rejects a shared graph on
  isolation grounds, and names the trigger for revisiting. The book has no
  equivalent discipline — and our reasoning ("a shared knowledge graph is by
  construction a cross-scope leak surface") is *correct against the book's own
  advice*. Hold the line.
- **Measurement honesty.** LADDER.md's "score by behavior, not license" and
  "never gate on raw agent count" have no counterpart in either source. Paperclip
  actively runs the other way.

---

## 3. Findings against `ai-standards` and the instance overlay

**S1 — The folder/remote rename outran its own handoff note, and six inbound
references were never swept.**

[`HANDOFF.md`](../../HANDOFF.md) (dated today) states the operational identifiers
were *deliberately* not renamed, and lists three: docker network, git remote,
folder. Two of those three are now wrong — `git remote -v` returns
`github.com/jgobuilds/ai-control-plane-public.git`, and the folder is `ai-control-plane`.
Only the docker network and `brightworks-*` workflow ids still carry the old
name, and **that part is a correct, deliberate deferral** — renaming them means
recreating the network and volumes with the stack down, and workflow ids are
load-bearing for cross-references. Leave them.

What was skipped is the inbound-reference sweep `change-safety` requires
("blast radius before delete/move — `grep -rn` for inbound references first; fix
them in the same change"). Six references to the old path survive across three
repos:

| File | Reference |
|---|---|
| `D:\code\CLAUDE.md` | a markdown link to `brightworks/AGENT-WORKSPACE.md` — **in the always-loaded ancestor file**, paid every session in every project |
| `<org>-instance/README.md` ×2 | `../brightworks` engine path; `../brightworks/scripts/check_public_hygiene.py` |
| `<org>-instance/onboarding/CLAUDE.local.md` | `../brightworks/docs/design/ARCHITECTURE.md` — the first file an agent reads at onboarding |
| `ai-standards/references/agent-context-architecture.md` | `../../brightworks/AGENT-WORKSPACE.md` |
| `ai-standards/ROLLOUT.md` | names the fragility of exactly this link shape, using this exact path |

The overlay's *resolution* logic is fine (sibling matching `*-instance`); its
prose dead-ends. Fix the six links and correct `HANDOFF.md`'s three-item list to
the one item that is still true.

**S2 — `ai-standards` has twelve lenses and none of them are about agents.**
The lenses are engineering-process lenses (change safety, dependency security,
cost awareness, concurrency, diagnosability…). Nothing reviews *an agent as a
designed artifact*. The book supplies a ready-made frame — the **five functional
layers** (persona, tools/actions, reasoning/planning, knowledge/memory,
eval/feedback), each with field-tested rules in ch. 11. `AGENT-DESIGN.md` in this
repo already covers persona, split, model, and handoff for one repo; promoting a
layered version to `ai-standards/references/agent-layers.md` makes it portable and
costs one file plus a line in `config.yml`. This is the single highest-value
addition either source suggests for the standards repo.

**S3 — No prompt-security lens exists at either level.** B1's content — instruction
hierarchy, untrusted-content rule, schema-first tool arguments with
`additionalProperties: false`, never-execute-user-content, allowlists over
denylists — belongs in `ai-standards/references/prompt-security.md`, because it
applies to every repo that ships an agent, not only to this one.

---

## 4. Priority

Ordered by (impact × confidence) ÷ cost. Items with two independent sources
behind them are marked ✦.

| # | Action | Source | Cost |
|---|---|---|---|
| 1 | **Instruction hierarchy + untrusted-content rule** in `workspace/CLAUDE.md` (book listing 8.8) | B1 | one section |
| 2 | **Container resource limits** — close M7 | B2 | one block per service |
| 3 | **Typed approval payload** — action, scope, risk dims, requester, preview | B3 | plumbing; router already computes it |
| 4 | ✦ **Authenticated approval ingress** — the remaining half of M5 | B10 + P4 | real work; unblocks the consulting story |
| 5 | **Amend MARKET-ANALYSIS.md** with the fifth category (agent work management) and Paperclip as its leader | §0 | one section |
| 6 | ✦ **Span-level tracing (OTel)** — promote from optional | B7 + Paperclip | moderate |
| 7 | **Execution lease** before a second concurrent lane targets the same repo | P2 | small, and it is already a stated rule we do not implement |
| 8 | **Confidence + stagnation signals** in `decision`; consider a quality-based breaker dimension | B4 | design work; sharpens EVAL.md's proxy |
| 9 | **Read Paperclip's sandbox adapters before writing the C1 silo runners** (`build-vs-adopt`) | P7 | reading time |
| 10 | **Add `agent-layers` + `prompt-security` lenses to `ai-standards`** | S2, S3 | two files |
| 11 | **Finish the Brightworks rename** — one sweep, workflow ids included | S1 | mechanical, but touches live n8n state |
| 12 | Record content-safety/moderation as **scoped out**, not missing | B9 | one line |

## What does not change

C1 and C2 remain the deep fixes. Neither external source moves them, and neither
offers a shortcut: Paperclip has no structural-isolation story at all, and the
book's own conclusion is that injection mitigations are "partial and layered
rather than complete" — which is the containment thesis C1 already encodes.
Everything above is the cheap layer that should hold while C1 lands.
