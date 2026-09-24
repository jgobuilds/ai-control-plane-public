# Skills — durable, reusable agent knowledge

The **continued-learning loop**: every time a review or an incident reveals
something an agent *should* have known, write it down here as a small skill file,
so future runs load it as context. This is "fix the harness, not the bug" made
concrete — the same move Snowflake used to turn individual learnings into org
tribal knowledge (~7,000 skills across ~1,000 engineers, feeding on-call KTLO from
30% down to a 5% target).

## Convention

- **One skill per file:** `skills/<area>-<slug>.md`.
- **Short and imperative:** the rule, when it applies, and a good/bad example.
- **Always-on rules** get referenced from `CLAUDE.md`; **situational** ones are
  pulled by area when relevant.
- **Skills are versioned with the repo.** When one turns out wrong, fix or delete
  it — a stale skill is worse than none.

## Maturity path (where this is heading)

Snowflake's on-call model is the target shape for automated streams:
1. **Encode** known procedures as skills (here).
2. **Event-driven execution** — hook skills to triggers (PagerDuty/Slack/monitoring
   → n8n → `/run`).
3. **Multi-step reasoning** — an LLM orchestrates the full response.
4. **Continuous learning** — feed each incident's discovery back into a skill.

## Example

`skills/n8n-slack-params.md`:
> **When** configuring the Slack node, **always** set `resource` and `operation`
> explicitly. **Why:** the defaults trigger a "missing parameter" error at runtime.
> This is the general rule (never rely on node defaults) applied to a node that
> bites people often.
