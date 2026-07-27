#!/usr/bin/env bash
set -euo pipefail

# Credential volume (~/.gemini) starts root-owned; fix before dropping privileges.
mkdir -p /home/node/.gemini /workspace
chown -R node:node /home/node/.gemini
chown node:node /workspace 2>/dev/null || true

# FAIL CLOSED: abort rather than run the agent with unrestricted egress.
if [ "${FIREWALL:-on}" = "on" ]; then
  if ! /app/init-firewall.sh; then
    echo "FATAL: firewall failed to apply (missing NET_ADMIN?). Refusing to start with open egress." >&2
    exit 1
  fi
fi

export HOME=/home/node

# One-off commands, e.g. interactive login:
#   docker compose run --rm gemini-runner gemini
if [ "$#" -gt 0 ]; then
  exec runuser -u node -- env HOME=/home/node "$@"
fi

exec runuser -u node -- env HOME=/home/node node /app/server.js
