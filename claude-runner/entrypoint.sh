#!/usr/bin/env bash
set -euo pipefail

# A named volume mounted at ~/.claude starts out root-owned and empty, which
# shadows the build-time chown. Fix ownership before we drop privileges so the
# node user can persist its login token.
mkdir -p /home/node/.claude /workspace
chown -R node:node /home/node/.claude
chown node:node /workspace 2>/dev/null || true   # best-effort on bind mounts

# Apply the egress allowlist (needs cap_add: NET_ADMIN). FIREWALL=off skips it.
# FAIL CLOSED: if the firewall is requested but can't be applied, abort — never
# run the agent with unrestricted egress.
if [ "${FIREWALL:-on}" = "on" ]; then
  if ! /app/init-firewall.sh; then
    echo "FATAL: firewall failed to apply (missing NET_ADMIN?). Refusing to start with open egress." >&2
    exit 1
  fi
fi

# runuser doesn't reset HOME by default, so set it explicitly — the CLI needs
# it to find ~/.claude.
export HOME=/home/node

# Allow one-off commands, e.g. the interactive login:
#   docker compose run --rm claude-runner claude login
if [ "$#" -gt 0 ]; then
  exec runuser -u node -- env HOME=/home/node "$@"
fi

exec runuser -u node -- env HOME=/home/node node /app/server.js
