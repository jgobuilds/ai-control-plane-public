# C1 + C2 deep fix — real structural isolation (scoping)

Turns the isolation guarantee from *advisory* (cwd-based, bypassable) into
*structural* (a runner physically cannot read what isn't mounted, and cannot be
reached to act on another scope). Addresses:

- **C1** — pool runner mounts the whole workspace; `cwd` isn't a boundary; Bash +
  `--dangerously-skip-permissions` reads any sibling scope.
- **C2** — `fanout`/incident/dependency call runners directly, skipping the
  router's scope/PII/conflict/tier enforcement.

## The core tension

Inheritance wants ancestors **readable** (enterprise + department context flows
down). Isolation wants siblings **unreadable** (client-A never sees client-B).
Any real fix has to give a scope its *ancestor chain* without its *sibling
branches*. A plain `mount the subtree` can't do that (mounting `enterprise/`
drags in every child). This is the whole problem; everything below serves it.

## Design principles

1. **The mount is the boundary.** Not `cwd`, not a prompt, not the model's
   goodwill. If a runner isn't mounted a directory, no amount of Bash or prompt
   injection reaches it. This is the only defense that survives
   `--dangerously-skip-permissions`.
2. **Enforce at the data (PEP), decide at the router (PDP).** The router stays
   the policy *decision* point (tier, cost, routing). Each runner becomes a
   policy *enforcement* point bound to one isolation root — so a direct call
   (C2) is safe-by-construction: the runner can only touch its own root.
3. **Defense in depth.** Even with perfect mounts, the runner re-checks scope
   and runs the PII guard itself, so nothing relies on the caller behaving.

## The model: isolation roots

An **isolation root** is a scope node that gets its own dedicated runner
container. A node becomes a root if any of: `isolation:"silo"`, `pii:"block"`,
membership in a `conflicts` pair, or an explicit `isolationRoot:true`.

- Everything **below** a root that isn't itself a root shares that root's runner
  (same trust domain — e.g. `data-team` and `jane` under an `engineering` root
  are *not* isolated from each other, which is fine: intra-department).
- Everything is under **exactly one** nearest isolation root → deterministic
  runner assignment.

This keeps the container count = number of confidentiality boundaries (a handful
of clients + a few domains), not one-per-node.

## Mount composition (the mechanic)

Each isolation-root runner mounts, generated from `scopes.json`:

- **read-only, ancestor own-content** — each ancestor's canonical shared slots
  (`CLAUDE.md`, `skills/`, `glossary.md`) bind-mounted at their nested path.
  *File/dir-level* binds, so ancestors come in **without their other children.**
- **read-write, the root subtree** — the root node and its non-root descendants.
- **nothing else** — sibling roots and other branches are absent from the
  filesystem entirely.

Concrete, for `client-a` (enterprise › consulting › client-a):

```yaml
volumes:
  - ./workspace/scopes/enterprise/CLAUDE.md:/workspace/scopes/enterprise/CLAUDE.md:ro
  - ./workspace/scopes/enterprise/skills:/workspace/scopes/enterprise/skills:ro
  - ./workspace/scopes/enterprise/consulting/CLAUDE.md:/workspace/scopes/enterprise/consulting/CLAUDE.md:ro
  - ./workspace/scopes/enterprise/consulting/skills:/workspace/scopes/enterprise/consulting/skills:ro
  - ./workspace/scopes/enterprise/consulting/client-a:/workspace/scopes/enterprise/consulting/client-a   # rw
```

cwd = `.../client-a`. Claude Code walks up for `CLAUDE.md` and finds
client-a → consulting → enterprise (all present); `client-b` and `engineering`
are simply not on disk. **Cross-scope read fails because the path doesn't exist.**

Convention this requires: ancestor *shared* content lives only in the canonical
slots (`CLAUDE.md`, `skills/`, `glossary.md`). Anything else in an ancestor dir
is not inherited. (Cheap to enforce; documented.)

> Cloud variant (GSUITE-GCP.md): bind mounts don't exist on Cloud Run — there you
> **materialize** the same effective tree into a per-root GCS prefix / volume.
> Same model, different plumbing.

## Runner changes (the PEP)

- New env `SCOPE_ROOT` (the node this runner serves). Mounts already limit reads;
  this makes rejection explicit and auditable.
- On `/run` **and** `/fanout`: resolve the request's `scope`; **403 unless it is
  `SCOPE_ROOT` or a descendant** within this runner's domain. A direct call for
  another scope is refused, not just unmounted.
- Run the **PII prompt guard** in the runner (move/duplicate the router's
  `piiScan` + `pii:block` logic) so it applies on every path, including direct
  `/fanout`. Closes C2's PII gap.
- `safeCwd` already blocks traversal; add the same validation to `fanout`'s
  `repo` param (fixes M3 in passing).

## Router changes (the PDP)

- `scopes.json` gains, per node, its resolved `runnerHost` (its nearest isolation
  root's runner). The router maps scope → that host for *all* dispatch, including
  a new `/fanout` passthrough so fan-out stops calling runners directly (C2).
- Router keeps tiering/cost/verify. It can no longer be "bypassed to a different
  root" because each root runner only serves its own root.

## Compose generation

Hand-maintaining N runner services + their mount lists is error-prone. Add
`scripts/gen-compose.mjs` that reads `scopes.json` and emits
`compose.scopes.yml` (one service per isolation root, with the ancestor-ro +
subtree-rw mounts and `SCOPE_ROOT`). Run it whenever the scope tree changes;
`docker compose -f docker-compose.yml -f compose.scopes.yml up`. The base file
keeps the shared infra (n8n, router, scrubber, agentsview); the generated file
owns the runners.

## Scrubber

The scrubber legitimately spans scopes (it writes each scope's ingest/
deliverables and holds the vault). It runs **no LLM and no agent** — it's trusted
infra, not an attack surface the way a runner is. **Decision:** keep it central
for now (documented residual: a scrubber compromise sees all scopes), and revisit
per-root scrubbers only if the threat model demands it. Its whole-workspace mount
is acceptable *because* it has no prompt-injectable component.

## What stays "pool" (and is now honest)

A single low-sensitivity **commons** runner may still serve a non-confidential
subtree (e.g. everything under `engineering`) — but it mounts **only that
subtree**, not the whole workspace. "Pool" now means "one trust domain, one
bounded mount," not "everyone shares everything." No scope with a confidentiality
boundary is ever pooled.

## Phased rollout

1. ✅ **Registry + generator (DONE)** — `scripts/gen_compose.py` derives roots
   from `scopes.json` (triggers: `silo|pii:block|conflict`, configurable via
   `isolationTriggers`) and emits `compose.scopes.yml` + `context/runner-map.json`.
   Inert until cutover. Sample tree → 5 root runners; ethical-wall invariant
   asserted (client-a/client-b never share a host).
2. **Runner PEP** — add `SCOPE_ROOT`, scope-within-root 403, in-runner PII guard,
   `repo` validation.
3. **Router** — scope→runnerHost map for all dispatch; `/fanout` passthrough.
4. **Workflows** — point `fanout`/incident/dependency at the router (not the
   runner) and pass `scope`.
5. **Cutover** — replace the single `claude-runner` with generated per-root
   runners; keep one commons runner for low-sensitivity scopes.

## Test plan (proves the fix)

The red-team test must now **fail closed**:
- From `client-a`'s runner, `Bash: cat /workspace/scopes/**/client-b/**` →
  no such path (structural).
- Direct `POST client-a-runner/run { scope: "client-b" }` → 403.
- `POST` any runner `/fanout` with a raw-PII prompt in a `pii:block` scope →
  refused by the in-runner guard.
- Ethical-wall pair can never resolve to the same runner host (assert in the
  generator).

## Cost / tradeoffs

- **+1 container per confidentiality boundary.** Runners share the one
  `claude-config` volume → still a single firm subscription login, no per-client
  auth. Idle memory is the only real cost; a handful of clients is trivial.
- **Generator adds a build step** when the tree changes — acceptable for the
  isolation it buys, and it removes hand-editing footguns.
- **Ancestor "canonical slots" convention** constrains where shared content
  lives — a small, documented discipline.

## Decisions (locked 2026-07-18)
- **D1 — root trigger: auto-derive.** A node is an isolation root iff
  `isolation:"silo"` OR `pii:"block"` OR it appears in a `conflicts` pair. No
  separate flag to forget; the controls you already set imply the boundary. The
  generator computes roots from `scopes.json`; no per-node bookkeeping.
- **D2 — lifecycle: always-on.** One long-lived container per root. All share the
  single `claude-config` volume (one firm subscription login). Idle memory is the
  only cost.
- **D3 — commons pool: bounded.** Scopes under no isolation root are served by one
  `commons` runner that mounts **only** the largest non-confidential subtree(s),
  never the whole workspace. Intra-domain scopes see each other (same trust
  domain, acceptable); nothing with a confidentiality boundary is ever pooled.

### Implied generator algorithm
```
roots = nodes where isolation==silo OR pii==block OR node ∈ conflicts.pairs
for each node N: N.runnerHost = nearest-ancestor-or-self in roots,
                 else "commons"
commons mounts   = each maximal subtree whose nodes all map to "commons"
root R mounts    = ancestors(R).ownContent(ro) + subtree(R)(rw)
assert: no conflicts pair shares a runnerHost   # ethical-wall invariant
```
