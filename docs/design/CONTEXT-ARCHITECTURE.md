# Context sharing & siloing

> **Generalized & implemented:** the three-layer model below is a configurable
> **scope tree** in [`context/scopes.json`](../../context/scopes.json) — any hierarchy
> (personal→team→department→enterprise, or org→counterparty→engagement) with
> per-level controls (pii, isolation, promotion, identity, retention). The router
> enforces it (scope validation, ethical walls, PII guard, cwd-scoped dispatch);
> the directory tree under `workspace/scopes/` *is* the hierarchy, so CLAUDE.md
> inheritance is structural. Cloud mapping: [`GSUITE-GCP.md`](../runbooks/GSUITE-GCP.md).

How to let agents share what should be shared (standards, frameworks, tools) and
never leak what shouldn't (work product belonging to one party, personal data).

## The three layers

| Layer | Content | Sharing rule |
|---|---|---|
| **L0 — Shared** | Standards, methodologies, tooling, house style, general skills | Every agent, every run |
| **L1 — Tenant** | Material belonging to one isolation unit, and that unit's tailoring of L0 | Visible ONLY within that unit; never crosses |
| **L2 — Restricted** | Personal data, financials, health, credentials inside L1 material | Locked down even *within* the unit; enters a prompt only by exception |

The governing idea, from multi-tenant AI isolation practice: **isolation is
enforced at four points — prompt, retrieval, tool call, response** — and the
obligation is that a request running for tenant A contains *no* data from tenant
B at any of them. Enforce it structurally (mounts, containers, filters), never by
asking the model to behave.

## What "tenant" means for your organization

L1 is the thing that must not leak into a sibling. **Everything else in this
document is unchanged by which one you pick** — that is the point of naming it
abstractly.

| Your situation | L1 is a… | Siblings that must not mix |
|---|---|---|
| Services firm, agency, contractor | tenant / engagement | two competing tenants |
| Enterprise, one company | department or function | HR vs the rest; Legal vs the rest |
| Anyone with external counterparties | counterparty | a supplier and its competitor |
| Deal or transaction work | deal team | buy-side and sell-side |
| Research + operating arms | business unit | research vs trading |
| Software vendor | customer tenant | any two customers |
| Regulated function | jurisdiction or entity | data that may not cross a border |

Two properties decide whether something is a genuine L1 rather than a folder:

1. **A leak between siblings is a reportable event**, not an inconvenience.
2. **Offboarding is a real operation** — one day you must be able to prove the
   material is gone.

If neither holds, it is a directory. Do not pay isolation costs for it.

---

## 1. Context layering — how sharing works

Use **composition at mount time**, not one knowledge pile with labels:

```
workspace/
├── CLAUDE.md              # L0: org-wide rules (always loaded)
├── skills/                # L0: org-wide skills (always loaded)
└── scopes/
    └── <tenant>/          # L1: mounted ONLY for that tenant's runs
        ├── CLAUDE.md      # tenant tailoring (voice, stack, constraints)
        ├── skills/        # tenant-specific skills
        ├── ingest/        # scrubbed source material (see PII pipeline)
        └── deliverables/
```

Claude Code merges context hierarchically (root `CLAUDE.md` + the working
directory's), so running an agent with `cwd = scopes/<tenant>/` gives it **L0 +
that tenant's L1 and nothing else**. Sharing is the default *only* at L0;
siloing is the default everywhere else — **an agent cannot leak a directory it
never had mounted.**

**Promotion is the only path from L1 → L0.** This is how the organization learns
without leaking: a tenant-specific skill that proves generally useful is
*promoted* — copied to `workspace/skills/` — only after (a) a PII and
identity scrub, (b) generalization (no names, numbers or recognisable
specifics), (c) human review. Treat promotion like open-sourcing internal code.

## 2. Isolation — three options (pick per risk tier)

The standard Silo / Pool / Bridge taxonomy for multi-tenant AI.

### Option A — Pool: one runner, per-run mount scoping (cheapest)

One runner; each request carries a `scope` field; the server maps it to
`cwd = /workspace/scopes/<tenant>` and the working directory is the boundary.
**Risk:** every tenant's files are in one container — a path-traversal prompt
(`read ../other-tenant/`) is one mistake away. Acceptable for low-sensitivity
tenants only, and only with the scope field **validated against an allowlist**,
never string-concatenated into a path.

### Option B — Silo: runner-per-tenant (strongest, recommended default)

A compose template stamps out `runner-<tenant>`, each mounting **only** that
tenant's scope plus the shared L0 (read-only). The container boundary and the
mount table enforce the wall — there is nothing to traverse to. Each can have its
own egress allowlist (tenant A's runner may reach tenant A's systems and nothing
else) and its own auth volume. Offboarding is deleting a container and a volume.
Cost: one container per active tenant — trivial next to token spend.

### Option C — Bridge: silo where it matters, pool for the rest

Option B for tenants under contractual or regulatory constraint, A for internal
or low-sensitivity work. **Any pair marked as a conflict MUST be siloed** — never
let two conflicting contexts share a container even in pool mode.

**Router enforcement (all options):** the router carries `scope` on every
request, validates it against the registry, and dispatches to that tenant's
runner (B) or scoped mount (A). Workflows never choose paths — they name a
scope; the router resolves it. One chokepoint, one auditable `decision` per call.

### Conflict pairs — the rule that is not about technology

Some sibling pairs must never share a resource *even when the technology would
allow it*: two competing tenants, buy-side and sell-side of one transaction,
research and trading, an investigation and its subject. The registry marks the
pair; the router refuses to schedule both into any shared resource.

This is a **policy input**, not an inference. An agent cannot deduce that two
tenants compete, and nothing in the file tree encodes it. Somebody has to
declare it, and the declaration has to arrive before the work does.

---

## 3. Application: departments inside one company

The pattern is unchanged; the tenants are internal.

- **L0** — engineering standards, house style, shared tooling, the security
  policy. Every department's agents load it.
- **L1** — one per department, or per function where confidentiality is
  asymmetric: **HR**, **Legal**, **Finance**, **Security**, and the ordinary
  delivery teams.
- **L2** — employee records, compensation, case files, incident detail.

What differs from an external-party setup:

- **The walls are asymmetric.** HR and Legal must not leak *outward*, but they
  often need to read L0 and sometimes another department's *output*. Model that
  as a read-only mount of a specific published directory, not as a widened wall.
- **Promotion is the main event.** In a single company most of the value is one
  team's practice becoming everyone's, so the promotion gate carries more
  traffic than the isolation does. Invest there.
- **The conflict pairs are subtler and more real than they look**: an
  investigation and the team being investigated, a reorganisation plan and the
  affected department, a compensation review and its subjects. Declare them
  explicitly — nobody will infer them.
- **Silo the few, pool the many.** Option C is almost always right internally:
  HR/Legal/Security siloed, delivery teams pooled with validated scoping.

## 4. Application: working with external companies

Any organization whose agents touch material belonging to another legal entity —
a services firm, an agency, a procurement team handling supplier bids, a company
with contractors inside its own tools.

- **L1 is the counterparty**, and the boundary is contractual, not just
  technical. What the contract says about where data may rest and who may
  process it **decides the option before any architecture does.**
- **Default to Option B.** When a leak is a breach of an agreement rather than an
  internal embarrassment, "the working directory was set correctly" is not a
  control anyone will accept afterwards.
- **Per-tenant egress is the underrated half.** Tenant A's runner reaching
  tenant B's systems is the same failure as reading their files, and a shared
  egress allowlist quietly permits it.
- **Offboarding is a deliverable.** At the end of an engagement you may have to
  state what was deleted and when. Silo mode makes that a volume deletion; pool
  mode makes it an argument.
- **Bidirectional risk.** Your own L0 — methods, pricing, internal standards —
  can leak *outward* into a deliverable. The promotion gate protects one
  direction; a review of what leaves protects the other.

---

## 5. PII controls — defense in depth

PII redaction belongs at **multiple points**: on ingest, on retrieval, and on the
assembled prompt right before the model call. Cheapest first:

1. **Scrub on ingest (deterministic, Tier-0).** When material enters
   `scopes/<tenant>/ingest/`, run a redaction pass —
   [Presidio](https://github.com/data-privacy-stack/presidio) (NER + regex +
   checksum validation) as a sidecar, or a regex layer for the easy classes
   (emails, phones, national IDs, cards). Replace with **deterministic tokens**
   (`«PERSON_003»`, `«EMAIL_001»`) so the text stays coherent for the model.
2. **Vault + rehydrate.** The token→value map lives in a per-tenant vault that is
   **never mounted into any runner**. If a deliverable needs real values back,
   rehydration happens *after* the model call, outside the agent, by a
   deterministic step. The model only ever sees tokens.
3. **Prompt-time guard (belt and braces).** The router runs a cheap deterministic
   PII scan on outbound prompts and blocks or warns on raw hits — catching
   anything that slipped ingest. Deterministic first; a model-based scan only if
   you need NER-grade coverage.
4. **Exception lane.** Work that genuinely requires real values (drafting a
   contract with real names) runs in a flagged lane: explicit `allowPii: true`,
   a human approval gate, and an audit record — mirroring the `allowFrontier`
   pattern already in the router.

**Non-negotiables regardless of option:** PII never in L0; never in promoted
skills; never in session logs if avoidable (scrub before the prompt, not after);
and a deletion story per tenant — silo mode makes the volume *be* the retention
boundary.

## 6. Control matrix

| Control | Layer | Mechanism | Already in stack? |
|---|---|---|---|
| Structural context scoping | L0/L1 | cwd + mount composition, hierarchical CLAUDE.md | ✅ pattern exists |
| Scope validated on every call | L1 | Router checks `scope` against the registry | Add to `router/policy.json` |
| Container-level walls | L1 | Runner-per-tenant (Option B), per-tenant egress allowlist | Template exists |
| Conflict pairs | L1 | Registry marks the pair; router refuses any shared resource | Add |
| PII scrub on ingest | L2 | Presidio sidecar / regex Tier-0, tokenize `«PERSON_N»` | Add |
| Vault + post-hoc rehydration | L2 | Token map outside all runners | Add |
| Prompt-time PII guard | L2 | Router outbound scan, block/warn | Add (chokepoint exists) |
| PII exception lane | L2 | `allowPii` + human gate + audit log | Mirrors `allowFrontier` |
| Promotion gate | L1→L0 | Scrub + generalize + human review | ✅ `promote-skill` + scrubber `/promote/*` |
| Audit trail | all | Router `decision` block + `status.html` | ✅ |
| Offboarding / retention | L1/L2 | Delete tenant volume + container + vault entry | Silo mode gives this free |

## 7. Choosing your shape

1. **Name your L1.** Use the table above. If a leak between siblings is not a
   reportable event, it is a directory, not a tenant.
2. **Sort tenants into two piles** — contractual/regulatory/conflicted, and
   everything else. The first pile is Option B; the second can pool.
3. **Declare the conflict pairs.** Nobody will infer them, and they arrive late
   if you wait to be told.
4. **Scrub at ingest before anything else.** It is the cheapest control and the
   only one that helps every downstream mistake.
5. **Put the registry behind the router.** One place knows the scopes, the
   conflict pairs, the PII guard and the exception lane — the same "policy at the
   chokepoint, not in the prompt" principle the cost tiering already uses.

The result: sharing is *structural* (what is mounted), siloing is *structural*
(what is not), and PII is *substituted, not trusted* — three mechanisms, none of
which depend on the model choosing to behave.

---
*Sources: [tenant isolation for LLM traffic](https://www.deepinspect.ai/blog/ai-tenant-isolation),
[multi-tenant agent architectures](https://omnithium.ai/blog/multi-tenant-agent-architecture.html),
[Silo/Pool/Bridge RAG isolation](https://www.maviklabs.com/blog/multi-tenant-rag-2026),
[strict isolation in RAG pipelines](https://truto.one/blog/how-to-architect-strict-data-isolation-in-multi-tenant-rag-pipelines/),
[Presidio PII detection](https://explainx.ai/blog/microsoft-presidio-pii-detection-anonymization-guide-2026),
[redact-and-rehydrate pipelines](https://appscale.blog/en/blog/pii-redaction-pipeline-llm-presidio-ner-reversible-tokenisation-2026).*
