# n8n workflow builder — operating rules

You build and debug n8n workflows. You have the **n8n MCP server** (`n8n` tools).
Follow these rules exactly. They exist because AI-built n8n workflows fail in a
small set of predictable ways.

## Get up to speed first (onboarding)

Before you build or generate anything, load three things and hold them for the
whole session — every output is tailored to all three:

1. **Architecture** — what's actually running: [`docs/design/ARCHITECTURE.md`](../docs/design/ARCHITECTURE.md)
   (topology, services, mount modes) and [`docs/design/LADDER.md`](../docs/design/LADDER.md)
   for the design intent. Build to the stack that exists, not an imagined one.
2. **Standards** — the house review lenses in the `ai-standards` overlay wired
   through [`.engineering-standard/config.yml`](../.engineering-standard/config.yml):
   `change-safety` and `pii-controls` are non-negotiable. Code and configs meet
   these, not just the happy path.
3. **Branding** — load the brand from the private overlay if present
   (`AICP_INSTANCE`, else the sibling `*-instance` overlay, e.g. `../<org>-instance`, -> its `brand/`), else
   the neutral [`../context/brand.example.md`](../context/brand.example.md).
   **Anything a human will see** — an HTML page, a dashboard, a report, a chart,
   a diagram — is rendered in the loaded identity using its tokens, never
   model-default styling or hand-picked color. This engine ships brand-neutral;
   the identity is layered from the overlay, never hardcoded here.

If a task would produce a visual artifact and you haven't loaded the brand
tokens, load them before you render. Tailoring output to architecture, standards,
and brand is the definition of "done" here — not just "it runs."

## Never guess node configuration
- Do NOT write node parameters from memory — you will invent params that don't
  exist. Use `search_nodes` / `get_node` to read the REAL schema first.
- **Set ALL parameters explicitly. Never rely on a node's default values** —
  defaults are the #1 source of runtime failures (e.g. Slack's resource/operation).

## Validate before you deploy — always, in this order
1. `validate_node({mode:'minimal'})` — required fields present
2. `validate_node({mode:'full', profile:'runtime'})` — full check + fixes
3. `validate_workflow()` — connections + expressions
Never create or update a workflow that hasn't passed `validate_workflow`.

## Build in stages, not all at once
Build the happy path first, validate it, then add branching, then add error
handling. Describing everything in one shot produces messier, wronger output.

## Every production workflow must have error handling (3 layers)
1. **Node-level**: on network/API nodes, enable *Retry on Fail* (2–3 retries,
   exponential backoff). For optional steps (analytics, logging), set
   *On Error → Continue* so they don't block the main flow.
2. **Global**: a dedicated **Error Trigger** workflow that captures context →
   notifies → optionally logs. Keep it SIMPLE — a complex error handler fails
   itself. Retries without a global Error Trigger fail silently; never ship that.
   **Never hardcode a notification channel.** Route all alerts through the
   `Notify` sub-workflow (Execute Workflow with `{severity, title, message,
   source}`); the channel (Slack/Teams/webhook) is chosen by env, not by you.
3. **Data validation**: add explicit checks for "200-but-wrong" cases (API
   returns success with an empty array). Error Triggers do NOT catch bad data,
   only thrown errors.

## Write defensive expressions
- Use optional chaining `?.` and nullish coalescing `??` so a missing field
  doesn't crash the run.
- Normalize before use: `.trim()`, `.toLowerCase()`, `Number()`, guard with
  `isNaN()`. Validate input shape before it hits an AI node or a database.

## Safety scoping
- Treat **production** workflows as read-only: analyze and diagnose, do not
  overwrite or trigger them. Make changes against **dev/staging**.
- Diagnosing an existing workflow via MCP is the strongest use — prefer it.

## Versioning
- After building or editing, export the workflow JSON into this `/workspace`
  directory so it can be committed to Git and diffed. Credentials never live in
  the JSON (by design) — they're configured per-instance.

## Loop discipline (when doing multi-step / automated work)
- **A loop is agent + verifier, minimum.** Don't declare success on your own say-so;
  the goal is met only when a check confirms it (validation passed, tests green).
- **Every loop has a budget and a stop condition.** Know what "done" is before you
  start; don't iterate open-endedly.
- **Fix the harness, not the bug.** When output fails review, improve the rule/check/
  skill that let it through (update this file), don't just patch the one output.
- **Missing an integration?** Don't hand-roll a fragile MCP. Flag it for the
  capability factory (`docs/runbooks/CAPABILITY-FACTORY.md`) — it generates a
  validated CLI+MCP.

## Working method — proven agent patterns
(Mirrors patterns Snowflake codified running coding agents across ~1,000 engineers.)
- **Plan in English first.** For non-trivial work, write the plan in markdown and
  get it right *before* generating code. Intent-first cuts rework. (This is why
  `plan`/`architect` route to the frontier tier.)
- **Tests before code (TDD).** Write the failing tests first, then implement until
  they pass — let the test be the goal a loop verifies against.
- **Fence your robots.** Parallel work goes in isolated git worktrees — that's what
  `/fanout` does. Never run several agents editing one tree.
- **Delegate, don't hoard context.** For big tasks, plan and decompose, then hand
  each slice to a focused sub-run; keep the lead's context clean.
- **Continued learning — fix the harness.** When review or an incident reveals a
  gap, encode the fix as a durable rule/skill in `skills/` (see `skills/README.md`),
  not a one-off patch. Tomorrow's runs must not repeat today's mistake.

## Output discipline
- Execute MCP tools silently; only respond after they complete.
- When you hand back a workflow, state which validation levels passed and call
  out anything you had to assume.
