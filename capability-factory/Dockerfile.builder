# Privileged build lane. Heavier than the runtime image (Go toolchain + Node +
# Claude Code + cli-printing-press). Used on-demand to GENERATE integrations,
# never to run production traffic.
# Node 22 is taken from the SAME pinned image the runtime services use, rather
# than from apt or a NodeSource install script. Reasons, in order:
#   1. bookworm's apt nodejs is Node 18, and @anthropic-ai/claude-code requires
#      >=22 — it installed with EBADENGINE warnings and then died at runtime on
#      `styleText` (a Node 22 API). That is what broke this build.
#   2. A `curl | bash` NodeSource installer would add an unpinned external script
#      to the highest-privilege image in the stack — exactly what
#      ai-standards/references/dependency-security.md says not to do.
#   3. Reusing the existing pin means one fewer artifact to track and review.
# node:22-bookworm-slim — pinned 2026-07-24, see context/image-policy.json
FROM node@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 AS node22

# golang:1.26-bookworm — pinned 2026-07-24, see context/image-policy.json
FROM golang@sha256:1ecb7edf62a0408027bd5729dfd6b1b8766e578e8df93995b225dfd0944eb651
# Both stages are bookworm/glibc, so the Node runtime copies across cleanly.
COPY --from=node22 /usr/local/bin/node /usr/local/bin/node
COPY --from=node22 /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
# Tooling printing-press uses (node now comes from the stage above, not apt).
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl git ca-certificates jq sqlite3 \
  && rm -rf /var/lib/apt/lists/*

# Claude Code (printing-press drives it) + the generator's slash commands/machine.
RUN npm install -g @anthropic-ai/claude-code \
  && curl -fsSL https://raw.githubusercontent.com/mvanhorn/cli-printing-press/main/scripts/install.sh | bash

# node user for parity with the runtime image; give it a home for ~/.claude.
# ORDER MATTERS: `node` is created FIRST so it can take uid 1000. Creating
# `builder` first claimed 1000, and the golang base (unlike the node base) has no
# pre-existing node user, so `useradd -u 1000 node` then died with "UID 1000 is
# not unique" and failed the build. That was masked for as long as the Node 18
# npm step above failed first — one broken step hiding the next.
RUN (id node >/dev/null 2>&1 || useradd -m -u 1000 node) \
  && (useradd -m -s /bin/bash builder 2>/dev/null || true) \
  && mkdir -p /home/node/.claude /generated /workspace \
  && chown -R node:node /home/node /generated /workspace

WORKDIR /generated
USER node
ENV HOME=/home/node

# Drop into a shell; run printing-press slash commands from inside Claude Code:
#   claude
#   > /printing-press <api-name-or-url>
CMD ["bash"]
