# workspace — the runtime context-scope tree

This directory is bind-mounted into the runners. It is **not** documentation
about the repo; editing it changes what agents can see at runtime.

## Guidance belongs in a scope, not here

A runner mounts **only its own scope subtree**. Anything parked at this level is
unreachable by every scoped runner — so operating rules written here reach
nothing, which is how ~2,600 tokens of n8n build guidance sat in this file
against zero readers.

Put guidance in the scope that does the work:

```
scopes/<business>/<practice>/CLAUDE.md     always-on rules for that practice
scopes/<business>/<practice>/skills/*.md   depth, pulled when relevant
```

Both are canonical mounted slots (`CLAUDE.md`, `skills`, `glossary.md` — see
`scripts/gen_compose.py`), so they arrive in the container automatically. The
sample tree under `scopes/enterprise/` shows the shape.

## Two things worth knowing before you write any of it

- **Links do not resolve.** A `CLAUDE.md` here cannot point at a repo doc, an
  overlay file, or anything above the mount. State the rule or put the depth in
  the scope's own `skills/`. An instruction that cannot be followed does not fail
  loudly — it gets approximated from memory.
- **Say less than you think.** Guidance written for older models tends to
  re-state judgment a current model already applies. When this file's n8n rules
  were tested by removing them, the agent still verified node types against the
  catalog, refused to guess three unknowable facts, and fixed an unreachable
  error branch. Spend the tokens on what an agent *cannot* work out from inside
  the container — host paths, sub-workflow contracts, which credentials are
  absent — and drop the rest.

## Work products

Runtime output (`ingest/`, `deliverables/`, `.fanout/`) is gitignored on purpose.
Anything an agent produces that is worth keeping goes in its scope and is pulled
back to version control by `scripts/harvest_workspace.py`, which reports first
and never commits.
