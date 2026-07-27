# Agent workspace & context layout

How to lay out folders, instruction files, and shared skills so agents get the
context they need and **nothing else**. This is the *working-set* axis:
what an agent can see and what it loads.

The *isolation* axis — who is allowed to see what, ethical walls, PII —
is [`CONTEXT-ARCHITECTURE.md`](CONTEXT-ARCHITECTURE.md). The two interlock:
this doc decides the **shape** of the tree, that one decides the **walls**
inside it. Read both before mounting real client data.

> **Vendor caveat.** Loading and precedence rules below are Claude Code's
> documented behavior. Codex-family tools reading `AGENTS.md` use a
> *different* mechanic (§7). Don't write one universal rule across both.

---

## 1. The rule that governs everything: where you launch

Almost every context problem is really a launch-point problem.

| Launch from | File access | Loaded **at launch** | Use when |
|---|---|---|---|
| Repo/workspace **root** | every file below it | root instructions only; subdirectory files load **on demand** | task spans packages |
| A **subdirectory** | that subtree only | that directory's file **plus every ancestor's** | work scoped to one package |

Two consequences people get wrong:

- **Ancestors are eager, descendants are lazy.** Every `CLAUDE.md` from your
  working directory *up* to the filesystem root loads in full at session start.
  Files *below* load only when the agent actually reads a file in that
  directory. This is what lets a 50-package tree stay affordable — and it is
  why an ancestor file is the expensive one to bloat.
- **Nested files don't survive `/compact`.** Only the project-root file is
  re-injected; subdirectory files simply reload next time that directory is
  touched. Don't put must-never-be-forgotten rules in a leaf.

**Default to launching narrow.** Launch at the package you're working in; widen
deliberately. A narrow launch is simultaneously cheaper (less eager context),
safer (smaller write blast radius), and more accurate (less irrelevant
instruction competing for attention).

## 2. One workspace folder vs per-project repos

The honest finding: **for context mechanics, monorepo vs many-repos barely
matters.** The directory walk doesn't know or care where a `.git` boundary is.
What actually determines cost and blast radius is *where you launch* (§1) and
*what you grant* (§3) — not how the code is versioned.

So choose layout on human grounds (release cadence, ownership, CI), then apply
the same context discipline either way.

That said, the **one parent folder holding many projects** pattern has a
specific, easily-missed property:

> A root instruction file in the parent is an **ancestor of every project**, so
> it is loaded eagerly into *every* session for *every* project underneath.

That is a feature — it's the cheapest possible shared-context mechanism, with
zero wiring — but it means the parent file must stay **small and universally
true**. Anything that applies to only some projects belongs in those projects.

`D:\code` is a working instance of this:

```text
D:\code\                          # parent workspace — NOT usually the launch point
├── CLAUDE.md                     # 53 lines. Universal house rules only.
│                                 #   ancestor of every repo → eagerly loaded, always
├── ai-standards/                 # shared review lenses, one canonical copy
│   └── references/
│       ├── environment-hygiene.md
│       └── agent-context-architecture.md
├── ai-control-plane/                  # ← launch here for Brightworks work
├── verdantground/
│   └── .engineering-standard/
│       └── config.yml            # overlay: ../../ai-standards   (explicit reference)
└── job-applications/
```

**Launch in the project, not the parent.** Launching at `D:\code` grants write
access to every project at once and buys nothing — the shared rules load from
the ancestor either way. Reserve the parent for genuinely cross-project work.

## 3. Granting access without leaking context

Access and context are **separate grants**, and conflating them is the main
cause of accidental context bloat:

| Mechanism | Grants file access | Loads instructions | Loads skills |
|---|---|---|---|
| `permissions.additionalDirectories` | ✅ | ❌ never | ❌ never |
| `--add-dir` / `/add-dir` | ✅ | only behind an env flag | ✅ automatically |

Use `additionalDirectories` when an agent needs to *read* a sibling library but
should not inherit its conventions. Use `--add-dir` when you genuinely want that
directory's skills in play. Reach for `permissions.deny` Read rules to keep
vendored and generated code out of results even when a search surfaces it.

**This is the same idea Brightworks already enforces at the container edge.**
A runner's bind mounts *are* its working set, and its `cwd` *is* its launch
point — the difference is that a mount table can't be talked out of, so the
boundary holds even against a prompt-injected agent:

| Concept | Local agent | Brightworks runner |
|---|---|---|
| Launch point | working directory | container `cwd` (router-resolved, allowlisted) |
| Working set | repo tree + added dirs | bind mounts in `docker-compose.yml` |
| Read-only reference | `additionalDirectories` | `:ro` mount (e.g. `./generated:/generated:ro`) |
| Hard wall | — | separate container + no mount at all (silo mode) |

Prefer the structural version wherever the data justifies it. An agent cannot
traverse to a directory that was never mounted.

## 4. What belongs in each layer

Put it in the **always-loaded instruction file** only if the agent should
*always* know it: conventions, build/test commands, and "never do X" rules.
Put it in a **skill** if it's reference material needed *sometimes*, or a
workflow you trigger by name.

The sharpest test comes from `/doctor`'s automated trim behavior: it removes
content the agent **can derive from the codebase** — directory layouts,
dependency lists, architecture overviews — and keeps **pitfalls, rationale, and
conventions that differ from tool defaults**. If the agent could learn it by
looking, don't spend always-loaded tokens on it.

### Documented budgets

| Layer | Budget |
|---|---|
| `CLAUDE.md` (each) | target **< 200 lines** — no hard cap, but longer files measurably reduce adherence |
| `SKILL.md` | **< 500 lines**; push detail into `reference.md` / `examples.md` / scripts |
| `MEMORY.md` | first **200 lines or 25 KB**, whichever comes first — the remainder is silently dropped |
| Skill listing | ~**1%** of the context window (tunable); per-skill description capped at 1,536 chars |
| Skill re-attach after compaction | **25,000** tokens combined, 5,000 per skill |

These are budgets for a reason. Chroma's *Context Rot* study (18 frontier models)
found accuracy degrading **non-uniformly, by 30–50% in places, well before the
stated context limit** — and worse with distractor content. Anthropic's own
context-engineering guidance converges on the same conclusion from the other
direction: a finite "attention budget," best spent on *"the smallest set of
high-signal tokens that maximize the likelihood of your desired outcome."*
Two independent parties reaching the same finding is unusually strong evidence.

**Progressive disclosure is the design response:** keep the always-loaded layer
small and high-signal; let depth be fetched just-in-time. A `SKILL.md` is best
understood as a table of contents, not a manual.

## 5. Sharing skills and context without drift

Ranked, best first:

| Mechanism | Use when | Notes |
|---|---|---|
| **Symlink** into `.claude/rules/` or `.claude/skills/` | one canonical folder, several projects, personal or small-team scale | Documented and supported: symlinks are resolved normally, circular links handled, and a target reachable from two places loads **once**. Windows needs admin/Developer Mode. |
| **Plugin** (+ marketplace) | shared content needs its own version history, or must cross repos/orgs | Namespaced `plugin-name:skill-name`, so it can never collide. The vendor-recommended answer once symlinking stops scaling. |
| **Ancestor instruction file** | universal house rules in a parent workspace | Free and automatic (§2), but eagerly loaded everywhere — keep it tiny. |
| ~~Git submodule~~ | — | **Avoid** for shared context. Updating one shared folder means updating every consuming repo, and versions drift apart. Well-documented pain, no agent-specific upside. |
| ~~Copy-paste~~ | — | **Never.** Guarantees silent divergence; stale context is worse than none (§8). |

> ⚠️ **Unattended runs can't see your personal folder.** Cloud, scheduled, and
> Cowork sessions do **not** read `~/.claude/skills/` from your machine — an
> invoked skill is simply reported missing. Anything that must run unattended
> has to be committed to the repo or packaged as a plugin. A personal-symlink
> setup that works interactively will fail silently in automation.

## 6. Precedence — three *different* mechanics, easily conflated

Getting this wrong produces a standard that's confidently incorrect:

1. **Instruction files are additive, not override.** All discovered `CLAUDE.md`
   files are **concatenated**, ordered root → working directory, so the nearest
   is read *last*. Nothing is replaced; conflicts are left to the model's
   judgment. (`CLAUDE.local.md` is appended after `CLAUDE.md` at each level.)
   Enterprise/managed policy loads first and **cannot be excluded**.
2. **Skill *name collisions* are hard overrides:** enterprise **>** personal
   **>** project. One definition wins outright.
3. **Rule *loading order* runs the other way:** user rules load *before* project
   rules, giving project rules higher effective priority in an additive context.

(2) and (3) look similar and point opposite ways — because one is an override
and the other is ordering. Nested monorepo skills are a fourth case again: a
same-named nested skill doesn't override, it stays available under a
directory-qualified name like `apps/web:deploy`.

## 7. Cross-vendor: `AGENTS.md`

`AGENTS.md` is now a genuine cross-vendor standard — contributed to the Linux
Foundation's **Agentic AI Foundation** (announced 2025-12-09) alongside MCP and
goose, with platinum members including AWS, Anthropic, Google, Microsoft, and
OpenAI.

**But Claude Code does not read it natively.** This is widely misreported;
several popular guides claim automatic dual-format reading. The documented
behavior is that you must **import or symlink** it:

```markdown
@AGENTS.md

## Claude Code
Use plan mode for changes under `src/billing/`.
```

Keep **one** source of truth and reference it. Maintaining parallel `AGENTS.md`
and `CLAUDE.md` files is the §8 duplication anti-pattern with extra steps.

Also note the mechanical difference: Codex-family tools use **closest-file-wins
override**; Claude Code **concatenates** (§6). A repo serving both should avoid
instructions whose correctness depends on which model resolved the conflict.

## 8. Anti-patterns

| Anti-pattern | Why it bites | Mitigation |
|---|---|---|
| **Bloated always-loaded file** | Degrades adherence and burns budget on every single session; ancestors are eager (§1) | Enforce the 200-line target; move detail into skills |
| **Documenting what's derivable** | Directory trees and dependency lists go stale and cost tokens to say what the agent could just look up | Keep rationale and gotchas; delete inventories |
| **Contradictions across layers** | Instruction files are additive, so both survive — and **the model may pick one arbitrarily** | Periodic review; prune irrelevant ancestors; treat edits as reviewable PRs |
| **Stale context** | Confidently wrong beats absent — the agent trusts it. Also *obsolete*: workarounds for old model limitations become pure overhead | Review after major model releases; a Stop hook can propose updates while the gap is fresh |
| **Secrets in context files** | Instruction files, rules, and skills have **no secret-scrubbing** — anything there is loaded verbatim into every session | Never put secrets in them at all. Use `.mcp.json` `${VAR}` expansion; keep non-shareable content in gitignored `CLAUDE.local.md` |
| **Over-broad working directory** | Grants write blast radius and eager context you didn't want | Launch narrow (§1); `additionalDirectories` for read-only siblings (§3) |
| **Copied shared context** | Diverges silently; no one notices until an agent acts on the stale copy | One canonical copy, symlinked or packaged (§5) |

## 9. Tool config placement (`.mcp.json`)

| Scope | Shared with team | Stored in |
|---|---|---|
| **Local** (default) | no | `~/.claude.json`, per project path |
| **Project** | **yes — commit it** | `.mcp.json` at repo root |
| **User** | no | `~/.claude.json` |

Precedence: local → project → user → plugin → connector. The winning entry is
used **whole**; fields are never merged across scopes.

- Commit **project** scope for team-shared servers; approval is still prompted
  before a project-scoped server from a cloned repo is used.
- Keep credentials out via `${VAR}` / `${VAR:-default}` expansion.
- ⚠️ **Footgun:** an unset variable with no default loads with a *warning*, not
  a failure — a missing secret degrades silently rather than stopping. Verify
  explicitly rather than assuming a broken config announces itself.
- Personal or experimental servers belong in **local** scope.

## 10. How Brightworks applies this

- `workspace/CLAUDE.md` (81 lines) is the shared **L0** layer, mounted into
  every runner — the ancestor-file pattern of §2, enforced by mount rather than
  by directory walk.
- `workspace/scopes/<scope>/CLAUDE.md` supplies per-scope tailoring; because the
  directory tree *is* the hierarchy, inheritance is structural (§1) rather than
  configured.
- `workspace/skills/` is the promotion target: skill promotion L1 → L0 is the
  governed version of §5's "one canonical copy," with scrub, generalization, and
  human review standing in for a package registry.
- The router's allowlisted `cwd` resolution is §1's launch point moved to a
  chokepoint an agent can't influence.
- Silo mode (runner-per-scope) is §3's blast-radius control taken to its
  structural limit: no mount, no traversal, nothing to negotiate with.

---

### Sources

Official Claude Code documentation (`code.claude.com/docs/en/`): [memory](https://code.claude.com/docs/en/memory),
[large codebases](https://code.claude.com/docs/en/large-codebases),
[skills](https://code.claude.com/docs/en/skills),
[MCP](https://code.claude.com/docs/en/mcp),
[plugin marketplaces](https://code.claude.com/docs/en/plugin-marketplaces).
Anthropic engineering, [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) (2025-09-29).
Chroma, [Context Rot](https://research.trychroma.com/context-rot) (2025-07) — 18 models, peer-reviewable, [code](https://github.com/chroma-core/context-rot).
[agents.md](https://agents.md); Linux Foundation, [Agentic AI Foundation](https://www.linuxfoundation.org/press/linux-foundation-announces-the-formation-of-the-agentic-ai-foundation) (2025-12-09).

*Contested / lower-confidence, flagged rather than asserted:* claims that Claude
Code natively reads `AGENTS.md` (contradicted by primary docs — §7); reported
`AGENTS.md` adoption counts (secondary sources only, not traced to a primary
count); the `WORKSPACE.md`-as-repo-registry pattern for multi-repo parents
(community practice, not first-party guidance — §2 deliberately relies on
ancestor loading instead). Anthropic documents no explicit "never put secrets in
`CLAUDE.md`" rule; §8's version is inferred from the absence of any scrubbing
mechanism, and is a documentation gap rather than a cited rule.
