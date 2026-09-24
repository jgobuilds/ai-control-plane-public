# Port Spotify's intra-session read routing, but delegate through the router — the hook decides when, `/route` decides what it costs

**Date:** 2026-09-12 · **Lens:** `ai-standards/references/cost-awareness.md`,
`build-vs-adopt.md`, `proving-controls.md`, `change-safety.md`

## Considered

Facts came from the vendor's own post, fetched to a file and quoted by line
(`research/raw/spotify-portal.txt`, read 2026-09-11), and from probes of this
system: `router/policy.json`, `claude-runner/server.js`,
`claude-runner/init-firewall.sh`, and a grep for hook configuration across the
tree. Figures below are Spotify's, on Spotify's corpus, and are labelled as such.

**The pattern.** A `PreToolUse` hook fires on every `Read`. Over a threshold
(their default: 350 lines) it **blocks** the call and redirects to a skill that
ships the file to a cheap worker model, which returns structured bullets; the
file itself never enters the frontier model's context
(`spotify-portal.txt:52`, `:74`). A second mode writes boilerplate straight to
disk (`:75`). Their worker is **Gemini 2.5 Flash** (`:19`) — this repo's `t1` is
`gemini-2.5-flash-lite`, so the pattern's cheap tier and ours are the same class
of model, chosen independently.

**What they claim, stated at their own confidence.** *"Tested against a Java
monorepo across four scenarios… Mean bulk-read savings were around a whopping
90%"* (`:85`). That is a **mean on bulk reads, on one monorepo, by the author** —
not an audited company-wide result, and they say the code-write case is *"harder
to measure"* (`:86`). **Nothing in this ADR relies on 90% being reproducible
here.** Three exclusions are stated by them, not discovered later: no delegating
edits (worker summaries carry unreliable line numbers, `:88`), no delegating
reasoning (the worker missed a thread-safety bug the frontier model caught,
`:89`), and nothing small — each delegation is a 10–30s round trip capped at 30s
(`:90`).

| Option | Cost | Verdict |
|---|---|---|
| **A. Port the pattern; delegate through `/route`** — hook blocks the read, skill calls the router with `task_type: extract`, which already resolves to `t1` | **$0 licence.** Real money: t1 tokens *instead of* t3 tokens — strictly less than today per delegated read, metered the same way. Non-dollar: a hook + a skill + a firewall rule + a third `boundary_check` rule to own; +10–30s per delegated read; coupling to Claude Code's hook API, a vendor surface that can change | **Adopt** |
| **B. Port it literally** — hook calls the worker model directly from the session | Cheapest to build (no firewall change, no router work). Non-dollar: **every delegated call becomes a model call this control plane never decided on** — no ledger record, uncounted by the budget breaker, unreachable by the killswitch, and **unscrubbed**, while the payload is whole file contents | **Reject** — it buys the saving by removing the governance that is the product |
| **C. Adopt `shunt` + Portal as shipped** (`claude plugin install shunt@portal`, `:108`) | $0 licence, but **requires a Portal instance with AiKA enabled** (`:49`, `:111`) — Portal is a Backstage-class platform. Unpriced for us; would need a costed spike, and the operational surface is the real bill | **Reject** — peer-sized infrastructure, and a **second model-routing authority** beside the router |
| **D. Instruction-only** — write the rule into `CLAUDE.md` and ask the agent to prefer cheap reads | $0 in money and effort | **Reject** — they tried exactly this first and it did not hold; see Because |
| **E. Do nothing** | $0 to build. Real money: every bulk read stays billed at frontier rate against a **capped** monthly credit, and is re-sent on every following turn | **Reject as the default**, though it remains correct until A is measured |

## Chose

1. **Port the pattern, not the plugin.** The idea is portable; `shunt` is bound to
   Portal, and Portal is not infrastructure this stack should acquire.
2. **The hook decides *when*; `/route` decides *what it costs*.** The delegation is
   an ordinary router call with `task_type: extract`, which `router/policy.json`
   already maps to `t1`. **No new tier, no new policy entry** — the integration
   point that matters already exists.
3. **Open runner → router egress, narrowly.** `init-firewall.sh` today permits
   inbound 8080, outbound 5678 to the n8n subnet, and outbound 443 to the
   allowlist. It does **not** permit the runner to reach the router, so option A
   does not work as the stack stands. One rule, scoped to the router's address and
   port.
4. **Extend `boundary_check.py` with a third rule *before* building the hook.** Its
   two rules are a URL naming a runner host, and `x-runner-token` in a send
   position. A hook calling a vendor API directly from inside a session is
   **neither**, so the enforcement cannot currently see the failure mode option B
   describes. Adding the capability before adding the temptation is the whole
   point of having a gate.
5. **Ship behind a threshold and measure on our own corpus.** No saving is claimed
   in this repo's docs until `eval/` carries a before/after on our files.

## Because

**The pattern's real finding is not the 90% — it is that advice did not work.**
They tried the rules as instructions in a `CLAUDE.md` first, and the model and the
engineers both routed around them; only a hard block at the tool boundary held.
That is this project's vacuity doctrine arriving from outside: a control that
merely advises is not a control, and a gate that cannot refuse is prose. Their
three-layer split — hooks enforce, scripts plumb, skills advise — is the same
shape as chokepoint / lane / documentation here, which is the strongest evidence
that the pattern belongs in this architecture rather than beside it.

**It governs a layer this stack does not currently reach.** The chokepoint is
`/route`, where work *enters*. Everything a session does *after* dispatch — every
`Read`, every tool call — is ungoverned by design, because the runner is
firewalled and scoped and that was judged sufficient. Cost is the first
requirement that crosses that line: the expensive decision is not which agent to
call, it is what the agent then pulls into context. This repo has **no Claude Code
hooks at all** today, so this is a new layer, not a change to an existing one.

**What we give up, stated plainly.** Latency: 10–30s per delegated read, a real
regression on interactive work and the reason the threshold must be tunable.
Coupling: `PreToolUse` is a vendor API, so this takes a dependency on a surface
Anthropic can change, and the hook must fail **open on error and closed on
policy** — a broken hook that blocks every read is worse than no hook. And
breadth: bulk reads are one slice of spend; this does nothing for long sessions
that are expensive because they are long.

**The trade in item 3 deserves naming rather than burying.** Runner → router
egress is new attack surface, and it points at the most privileged service in the
stack. Two things make it the right call. First, the direction is *toward* the
chokepoint, not around it — the dangerous topology is a runner reaching a vendor
or a peer runner unmediated, which is what option B does and this prevents.
Second, the precedent already exists: `ask-human-mcp.js` reaches the ask-human
door on 5678, so a runner calling back into the control plane through a narrow,
governed door is an established shape here, not a new one.

**The residual risk is escalation, and it is not closed by this ADR.** Once a
runner can originate a `/route` call, a compromised or confused session can ask
the control plane to do work — potentially naming another scope. The router
enforces scope and RBAC on every call, so the check exists; what does not yet
exist is a distinct **requester identity for runner-originated requests**, so that
such a call can never inherit the human requester's authority and can be refused,
rate-limited and audited as its own class. That work is part of the build in
Status, not an afterthought, and it is why this is an ADR rather than a commit.

**Why this matters more here than it did there.** Their argument is cost. Ours is
capacity: per [`PROVIDER-BILLING.md`](../design/PROVIDER-BILLING.md), `claude -p`
draws on a **capped** monthly Agent SDK credit and requests *stop* at the ceiling
unless usage credits are enabled. A token saved is not money back, it is headroom
against a hard wall — which makes a bounded, measurable reduction worth more than
the same percentage would be on a metered account.

## Status

**Decided, not built.** Verified by probe, not assumed: `task_type: extract`
already resolves to `t1` in `router/policy.json` (12 task types, read 2026-09-12);
the runner invokes `claude -p` via `execFile` with `--mcp-config` and
`--allowedTools` (`claude-runner/server.js:183-201`), so a hook config can be
mounted alongside; and `init-firewall.sh` does **not** currently allow
runner → router, which is the one hard blocker. Unverified and assumed: that a
`PreToolUse` hook fires as documented inside a `--dangerously-skip-permissions`
session — this must be probed on the live stack before anything else is built,
because if it does not fire there, option A is dead and E stands.

Build order, smallest provable step first:

1. Probe that a `PreToolUse` hook fires at all in a runner session. **Stop if not.**
2. Third `boundary_check` rule, plus a test that synthesises the violation and
   demands the gate go red — the capability lands before the temptation.
3. Runner-originated requester identity in the router, with its own RBAC class.
   **This step also has to close the router half of threat-model H5.** The risk
   model scores blast radius from the caller's declared `action`, and an omitted
   verb scores as `advise`. For today's internal n8n callers that is
   defense-in-depth. For a runner-originated call it is the requester
   choosing its own gates: a session that declares `advise` skips
   `requireVerify` and `requireApproval`. A runner-class request must have its
   tools bound to its effective action (`risk.enforce.actionBinding: block` for
   that class at least) before step 4 opens the firewall.
4. Narrow firewall rule.
5. Hook + skill, threshold configurable, failing open on error.
6. A before/after measurement on this repo's own files, into `eval/`.

**Revisit when:** the probe in step 1 fails; or measured savings on our corpus come
in under ~30% on bulk reads, at which point the latency and the maintained hook are
not worth the headroom; or Anthropic ships first-party model routing inside the
session, which would make all of this vendor-supplied and turn this ADR into a
decline-and-adopt.

## Sources

- Dimitri Mazmanov, *"Portal by Spotify cut my Claude Code token usage by 90%"*,
  Spotify Engineering, 2026-09-03 — fetched 2026-09-11 to
  `research/raw/spotify-portal.txt`; every figure above cites a line in that file.
  <https://engineering.atspotify.com/2026/9/portal-by-spotify-cut-my-claude-code-token-usage-by-90>
- This repo, probed 2026-09-12: `router/policy.json`, `claude-runner/server.js`,
  `claude-runner/init-firewall.sh`, `scripts/boundary_check.py`.
