# HANDOFF — naming and brand separation

**Superseded 2026-07-26.** The original version of this file described a
2026-07-25 rebrand to "Brightside AI Control Plane" and told readers *not* to
rename the folder, the remote, or the Docker network. All three have since been
renamed, and the product name has been reverted, so every operational claim in
that version was wrong. It is replaced rather than amended, because a handoff
doc that is half true is worse than none.

## What is true now

| Thing | Value |
|---|---|
| Product name | **AI Control Plane** — no org prefix |
| Folder | `ai-control-plane/` (was `brightworks/`) |
| Git remote | `github.com/jgobuilds/ai-control-plane-public` |
| Docker network | `ai-control-plane_agentnet` |
| Endorsement | "Built and maintained by JGOBuilds" |
| Copyright | **Brightside Data LLC** — the legal entity, unchanged |

The n8n workflow ids still carry the `brightworks-` prefix (`brightworks-notify`,
`brightworks-approval-gate`, …). **Leave them.** They are stable identifiers that
cross-workflow references resolve against; renaming them breaks every reference
for no gain. An id is not a brand.

## The separation that matters

This repo ships **brand-neutral** and is intended to become public. Real org
identity — palettes, fonts, org name, marketing pages — lives in a private
overlay resolved generically at `../<org>-instance` (or `$AICP_INSTANCE`), never
here. `scripts/check_public_hygiene.py` enforces it at commit time and in CI.

Two consequences people trip on:

- **Your checkout may render branded** while the repo contains no brand. That is
  the overlay working, not a leak. With no overlay present the engine falls back
  to the neutral `context/*.example.*` files and renders unbranded.
- **Copyright is not branding.** The notices name Brightside Data LLC because a
  legal entity has to hold the rights; that is independent of what the product is
  called or who endorses it. A GitHub handle cannot hold copyright.

## If the product name changes again

Change the presentation only: `README.md`, `QUICKSTART.md`, and the one line in
`scripts/check_public_hygiene.py`'s docstring. Do **not** rename the folder, the
remote, the Docker network, the container names, or the workflow ids as part of
it — that is a separate, sequenced operation (stack down, recreate network and
volumes, rename the repo) and mixing the two is how the previous version of this
file ended up wrong.
