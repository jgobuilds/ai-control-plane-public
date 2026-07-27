# Context sharing & siloing — architecture for a consulting company

> **Generalized & implemented:** the L0/L1/L2 model below is now a configurable
> **scope tree** in [`context/scopes.json`](../../context/scopes.json) — any hierarchy
> (personal→team→department→enterprise, or enterprise→client→engagement) with
> per-level controls (pii, isolation, promotion, identity, retention). The router
> enforces it (scope validation, ethical walls, PII guard, cwd-scoped dispatch);
> the directory tree under `workspace/scopes/` *is* the hierarchy, so CLAUDE.md
> inheritance is structural. GSuite/GCP mapping: [`GSUITE-GCP.md`](../runbooks/GSUITE-GCP.md).

How to let agents share what should be shared (branding, frameworks, tools) and
never leak what shouldn't (client work product, PII). Three layers with
different sharing rules:

| Layer | Content | Sharing rule |
|---|---|---|
| **L0 — Firm** | Branding, methodologies/frameworks, tools, house style, general skills | Shared with every agent, every engagement |
| **L1 — Client** | Transcripts, deliverables, client-specific tailoring of L0 | Visible ONLY to that client's engagements; never crosses clients |
| **L2 — PII** | Names, contacts, financials, health data inside client material | Locked down even *within* the engagement; enters a prompt only by exception |

The governing idea (from multi-tenant AI isolation practice): **isolation is
enforced at four points — prompt, retrieval, tool call, response** — and the
core obligation is that a request running for client A contains *no* data from
client B at any of those points. Enforce it structurally (mounts, containers,
filters), never by asking the model to behave.

---

## 1. Context layering — how sharing works

Use **composition at mount time**, not one big knowledge pile with labels:

```
workspace/
├── CLAUDE.md              # L0: firm-wide rules (always loaded)
├── skills/                # L0: firm-wide skills (always loaded)
└── engagements/
    └── <client>/          # L1: mounted ONLY for that client's runs
        ├── CLAUDE.md      # client tailoring (voice, stack, constraints)
        ├── skills/        # client-specific skills
        ├── transcripts/   # scrubbed source material (see PII pipeline)
        └── deliverables/
```

Claude Code already merges context hierarchically (root `CLAUDE.md` + the
working directory's `CLAUDE.md`), so running an agent with
`cwd = engagements/<client>/` gives it **L0 + that client's L1 and nothing
else**. Sharing is the default *only* at L0; siloing is the default everywhere
else — an agent can't leak a directory it never had mounted.

**Skill promotion is the only path from L1 → L0** (this is how the firm learns
without leaking): a client-specific skill that proves generally useful gets
*promoted* — copied to `workspace/skills/` — only after (a) PII/client-identity
scrub, (b) generalization (no client names, numbers, or recognizable
specifics), (c) human review. Treat promotion like open-sourcing internal code.

## 2. Client isolation — three options (pick per risk tier)

Mirrors the standard Silo / Pool / Bridge taxonomy for multi-tenant AI:

### Option A — Pool: one runner, per-run mount scoping (cheapest)
One `claude-runner`; each request carries a `client` field; the server maps it
to `cwd = /workspace/engagements/<client>` and the OS working directory is the
boundary. **Risk:** all clients' files are in one container — a path-traversal
prompt (`read ../other-client/`) is one mistake away. Acceptable for low-
sensitivity clients only, and only with the client field **validated against an
allowlist** (never string-concatenated into a path).

### Option B — Silo: runner-per-client (strongest, recommended default)
Compose template stamps out `claude-runner-<client>`, each mounting **only**
`engagements/<client>` + the shared L0 (read-only). The container boundary and
the mount table enforce the wall — there is nothing to traverse to. Each can
have its own egress allowlist (client A's runner may reach client A's Jira and
nothing else) and its own auth volume. Offboarding = delete the container +
volume (clean retention story). Cost: one container per active client — trivial
next to the token spend.

### Option C — Bridge: silo for regulated clients, pool for the rest
Run B for clients under NDA/regulatory constraints, A for internal or
low-sensitivity work. This matches consulting reality: your "ethical wall"
clients (competing clients in the same industry) MUST be siloed — never let two
competitors' contexts share a container even in pool mode.

**Router enforcement (all options):** the router carries `client` on every
request, validates it against the engagement registry, and dispatches to that
client's runner (B) or scoped mount (A). n8n workflows never choose paths —
they name a client; the router resolves it. One chokepoint, auditable
`decision` block per call.

## 3. PII controls — defense in depth

PII redaction belongs at **multiple points**: on ingest, on retrieval, and on
the assembled prompt right before the model call. Options, cheapest first:

1. **Scrub on ingest (deterministic, Tier-0).** When a transcript enters
   `engagements/<client>/transcripts/`, run it through a redaction pass —
   [Presidio](https://github.com/data-privacy-stack/presidio) (formerly `microsoft/presidio`; spun out to the Data Privacy Stack org — the old path now redirects) (NER + regex +
   checksum validation, e.g. it validates credit-card checksums) as a small
   sidecar container, or a `rules.js`-style regex layer for the easy classes
   (emails, phones, SSNs, cards). Replace with **deterministic tokens**
   (`«PERSON_003»`, `«EMAIL_001»`) so the text stays coherent for the model.
2. **Vault + rehydrate.** The token→value map lives in a per-client vault file
   (or proper vault) that is **never mounted into any runner**. If a deliverable
   needs real names back, rehydration happens *after* the model call, outside
   the agent, by a deterministic step (n8n Code node or a script) — the model
   only ever sees tokens. This is the standard "anonymize before the provider,
   rehydrate on the way back" pattern.
3. **Prompt-time guard (belt and suspenders).** The router runs a cheap
   deterministic PII scan on outbound prompts and *blocks or warns* on raw
   hits — catching anything that slipped ingest. Deterministic first (regex,
   free), model-based scan (t1) only if you need NER-grade coverage.
4. **Exception lane.** Work that genuinely requires PII (e.g. drafting a
   contract with real names) runs in a dedicated flagged lane: explicit
   `allowPii: true` on the request, human approval gate in n8n, logged in the
   audit trail — mirroring the `allowFrontier` pattern already in the router.

**Non-negotiables regardless of option:** PII never in L0; never in promoted
skills; never in agentsview-visible session logs if you can avoid it (scrub
before prompt, not after); deletion story per engagement (silo mode makes this
easy — the client's volume *is* the retention boundary).

## 4. Control matrix (summary)

| Control | Layer | Mechanism | Already in stack? |
|---|---|---|---|
| Structural context scoping | L0/L1 | cwd + mount composition, hierarchical CLAUDE.md | ✅ pattern exists (`/workspace` cwd) |
| Client validated on every call | L1 | Router checks `client` against engagement registry | Add to `router/policy.json` |
| Container-level walls | L1 | Runner-per-client (Option B), per-client egress allowlist | Template exists (clone runner service) |
| Ethical walls (competitor pairs) | L1 | Registry marks conflicts; router refuses to schedule both into any shared resource | Add |
| PII scrub on ingest | L2 | Presidio sidecar / regex Tier-0, tokenize `«PERSON_N»` | Add (fits `rules.js` philosophy) |
| Vault + post-hoc rehydration | L2 | Token map outside all runners; rehydrate after model call | Add |
| Prompt-time PII guard | L2 | Router outbound scan, block/warn | Add (router chokepoint exists) |
| PII exception lane | L2 | `allowPii` + human gate + audit log | Mirrors `allowFrontier` |
| Skill promotion gate | L1→L0 | Scrub + generalize + human review before copying to firm skills | ✅ `promote-skill` workflow + scrubber `/promote/*` (ancestor-only, PII re-check, never auto-approved) |
| Audit trail | all | Router `decision` block + agentsview | ✅ |
| Offboarding/retention | L1/L2 | Delete client volume + container + vault entry | Silo mode gives this for free |

## 5. Recommended shape for a consulting firm

**Option C (Bridge) + ingest-time scrub + vault rehydration:**

- L0 lives in `workspace/` (read-only mount everywhere) — firm skills grow via
  the promotion gate.
- Every regulated/NDA/conflicted client gets a **siloed runner** with only
  their engagement mounted; everyone else pools with validated per-run scoping.
- All transcripts pass **Presidio-style scrub → tokens** before they ever land
  in an engagement folder; token vaults stay outside agent reach; rehydration
  is a deterministic post-step.
- The **router** is the one place that knows the engagement registry, enforces
  client validation, ethical-wall pairs, the PII prompt guard, and the
  `allowPii` exception lane — the same "policy at the chokepoint, not in the
  prompt" principle the cost tiering already uses.

The result: sharing is *structural* (what's mounted), siloing is *structural*
(what isn't), and PII is *substituted, not trusted* — three different
mechanisms, none of which depend on the model choosing to behave.

---
*Sources: [tenant isolation for LLM traffic](https://www.deepinspect.ai/blog/ai-tenant-isolation),
[multi-tenant agent architectures](https://omnithium.ai/blog/multi-tenant-agent-architecture.html),
[Silo/Pool/Bridge RAG isolation](https://www.maviklabs.com/blog/multi-tenant-rag-2026),
[strict isolation in RAG pipelines](https://truto.one/blog/how-to-architect-strict-data-isolation-in-multi-tenant-rag-pipelines/),
[Presidio PII detection](https://explainx.ai/blog/microsoft-presidio-pii-detection-anonymization-guide-2026),
[redact-and-rehydrate pipelines](https://appscale.blog/en/blog/pii-redaction-pipeline-llm-presidio-ner-reversible-tokenisation-2026).*
