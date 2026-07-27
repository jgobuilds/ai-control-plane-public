# Slack integration: how a governed agent platform meets "AI agents in Slack"

**Date:** 2026-07-23 · **Lens:** `ai-standards/references/build-vs-adopt.md`,
`ai-standards/references/change-safety.md`

## Considered

Research is from Slack's own properties only (`slack.com/help`, `docs.slack.dev`,
`slack.com/blog`); anything community-sourced was excluded from load-bearing
claims. Two candidate integration *shapes* exist, and they are not exclusive.

| Shape | Cost | What it is | Effort for us | Governance seam |
| --- | --- | --- | --- | --- |
| **Path A — Bolt agent app** | $0 to build; **some Slack AI features need a paid plan**; free Developer Program sandbox | We build a Slack app (`agent_view`, Assistant class, Socket Mode); the agent "lives in" a Slack thread. Push UX. | High — a new Slack-specific surface: manifest scopes, streaming (`chat.startStream`), suggested prompts, feedback buttons. | Bolted *around* Slack; we wrap our controls at the app edge. |
| **Path B — Slack MCP server as a tool source** | $0 to build; same paid-plan gate + **directory-published or internal app** required | Register Slack's remote MCP server (`https://mcp.slack.com/mcp`) as a tool source for a Runner. Pull UX — the agent reaches *into* Slack as a governed tool call. | Low — our Runners (Claude Code, Gemini CLI) are already MCP clients. | **Flows through the router we already have** — approval gate, audit ledger, PII scrub apply unchanged. |
| Salesforce Agentforce | **Salesforce-licensed** — separate commercial product | Slack-native but Salesforce-authored/licensed. | n/a | Not our surface — only relevant to interop with a customer's existing Agentforce agent. |
| Marketplace vendor agents (Claude/ChatGPT/Perplexity apps) | vendor-priced per app (consumer subscriptions) | Consumer-installable. | n/a | Not something we build. |

Confirmed constraints (all from `docs.slack.dev`):

- **Socket Mode** removes the need for a public inbound URL (outbound WebSocket
  via an app-level `xapp-` token) — fits the router-behind-firewall model. *Not*
  explicitly documented as compatible with `agent_view` event delivery; general
  Socket-Mode/Events-API support is confirmed, the agent-specific pairing is
  "not contradicted" — pilot before committing.
- **Paid plan** required for *some* AI features (stated verbatim, not itemized).
  Free **Developer Program** sandbox covers build/test.
- **Distribution gate**: RTS search and the MCP server both require a
  "directory-published *or* **internal** app" — internal status qualifies, so no
  Marketplace listing is forced.
- **Guests are hard-blocked** from agent-enabled apps (platform rule, not a
  toggle).
- **`agent_view` is current; `assistant_view` (split-pane) is deprecating** —
  build against `agent_view` if we ever take Path A.

## Chose

1. **Adopt Path B first** — Slack's MCP server as a governed tool source for a
   Runner. This is the primary integration.
2. **Defer Path A** (Bolt agent app) until there is a concrete product need for
   the "agent is a visible teammate in a Slack thread" UX. Do not build it
   speculatively.
3. **Treat Slack as an external, bidirectional system boundary** — like the
   existing Notify webhook channel, but now data flows *in* as well as *out*.
4. **Make four governance controls hard requirements** before any Slack call
   ships (see *Because*).

## Because

**Path B is both lower-effort and the better architectural fit.** A Slack tool
call is just another governed action the router already mediates, so the audit
ledger, PII tokenizer, and human-approval gate wrap it for free — no second
governance surface to build and keep in sync. Path A's only advantage is push
UX, which is a product decision, not a technical one; building it now would be
speculative surface area (build-vs-adopt: don't build what you don't yet need).

**The four governance controls, in priority order:**

1. **Per-tenant isolation is the sharpest mismatch.** Our unit of isolation is
   the *tenant*; Slack's is the *workspace/Grid org*, with channel membership as
   the finer control. No Slack primitive maps 1:1 to a tenant. **The router must
   own the mapping** — one Slack install per tenant workspace, refusing
   cross-tenant channel access *before* any Slack call — because a correctly
   scoped bot token can otherwise read any channel it belongs to.
2. **The audit chain degrades at the Slack boundary.** The hash-chained ledger
   can record "we asked Slack to post X, Slack acknowledged `ts`", but that is
   not cryptographic anchoring on Slack's side. Record the API response
   (`channel`, `ts`) as the linkage record and state the custody limit honestly.
3. **PII must scrub *both* directions.** Outbound: tokenize before content
   crosses into Slack. **Inbound is the commonly-missed half** — Slack channels
   hold arbitrary employee-entered PII; RTS/MCP search results must pass through
   the *same* scrubber before reaching Runner context. Slack's own
   best-practices doc also flags prompt-injection / exfiltration on inbound
   content — route it through the policy layer, never straight into context.
4. **Data leaving the boundary is subject to Slack-side retention** we don't
   control. The ledger can prove we *sent* X and its hash; it cannot govern what
   Slack keeps.

**Billing note (carries over from `0001`):** Runners use subscription/free-tier
auth. Slack's "paid plan for some AI features" is a *Slack*-side license cost,
separate from our zero-marginal-token runner model — budget a paid pilot
workspace, mitigated by the free Developer Program sandbox for build/test.

## Status

Decision recorded; **not yet implemented.** Path B pilot is the next build step
when prioritized. Open items to verify during the pilot: exact minimal scope
bundle (Slack's docs never enumerate it — read it off the manifest the Agents
toggle generates), the plan tier each sub-feature gates on, and Socket-Mode ×
`agent_view` compatibility in practice.
