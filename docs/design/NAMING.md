# Agent naming convention

**Default: `{Name} the {Role}` — the given name alliterates with the role word,
and the role states the goal in plain language.** Read the pair and you know what
it does: *Alice the Analyst*, *Vera the Verifier*, *Rex the Registrar*.

## Rules

1. **Alliterate the name to the role** (Alice/Analyst, Sam/Scrubber). The
   matching initial is the whole mnemonic.
2. **The role is the source of truth** — it names the *goal/function*, not the
   technology (`Reviewer`, not `sonnet-node`). If someone reads only the role,
   they still know the job.
3. **Multi-word roles alliterate to the salient word** — *Petra the
   PII-Protector*, *Ollie the Orchestrator*.
4. **Keep a registry** (below) so names are stable and unique — a name is an
   identity people trust across notifications, dashboards, and the audit ledger.
   Vary the names across the roster; avoid names of real teammates or well-known
   figures.
5. **A persona is how an agent CLAIMS work.** Under
   [ADR 0012](../decisions/0012-work-dispatch.md) a ticket is claimed with an
   `agent:<persona-slug>` label — lower-cased and hyphenated, e.g.
   `agent:dex-the-dispatcher`. This is the one place a persona crosses into
   machinery, and it stays consistent with rule 6: the label is *human-facing*,
   the thing a person reads to see who holds a ticket. It is deliberately NOT
   the GitHub assignee. Assignees must be real accounts, so a persona cannot be
   one — and `gh` exits 0 when it rejects an assignee, meaning a claim would
   report success while claiming nobody. The assignee keeps its ordinary
   meaning: the accountable human.

6. **Personas ≠ identifiers.** Docker service names, container names, and code
   (`router`, `claude-runner`, `scrubber`) stay as-is. The persona is a
   human-facing **label** — carried in config as an optional `agentName`, shown
   in Notify messages and decision summaries, never used as a technical key.

To coin one: pick the role word → pick a common given name with the same initial.

## Registry — Brightworks roster

| Persona | Role / goal | Brightworks component |
|---|---|---|
| **Rita the Router** | Route each request to the cheapest capable model | `router` |
| **Vera the Verifier** | Independent fresh-context review of agent output | verify pass / cross-provider verify |
| **Petra the PII-Protector** | Tokenize sensitive values in, rehydrate out | `scrubber` |
| **Gwen the Gatekeeper** | Hold irreversible actions for human approval | `approval-gate` workflow |
| **Ollie the Orchestrator** | Fan work out to isolated worktree agents | `/fanout` |
| **Iggy the Incident-responder** | Diagnose alerts, propose a gated fix | `incident-responder` lane |
| **Dana the Dependency-updater** | Weekly security/minor dependency bumps | `dependency-bumps` lane |
| **Della the Deliverer** | Rehydrate and ship approved deliverables to Drive | `deliverables` lane |
| **Sydney the Syncer** | Pull Drive content through the scrubber into scope | `drive-sync` lane |
| **Percy the Promoter** | Scrub, review, and promote skills up the scope tree | `promote-skill` lane |
| **Casey the Circuit-breaker** | Halt agent activity on operator halt or spend/rate trip | `killswitch` |
| **Auggie the Auditor** | Append each decision to the tamper-evident ledger | audit ledger |
| **Rex the Registrar** | Keep the AI use-case conformity inventory current | use-case register |
| **Dex the Dispatcher** | Claim one ready ticket and hand it to the router | `dispatch` lane |

Per-scope working agents take a persona too — see `agentName` in
`context/scopes.json` (e.g. **Ada the Advisor** for `client-a`, **Dana the
Data-analyst** for `data-team`). The role should match the scope's `useCase`.

> The registry is a starting roster — swap any persona for one your team prefers;
> the convention (`{Name} the {Role}`, alliterative) is the durable part.
