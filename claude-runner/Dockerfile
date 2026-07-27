# node:22-bookworm-slim — pinned 2026-07-24, see context/image-policy.json
FROM node@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3
# iptables/ipset: egress firewall. dnsutils: dig (resolve allowlist). curl:
# healthcheck. git/bash: worktree-per-agent fan-out + self-verify commands.
RUN apt-get update && apt-get install -y --no-install-recommends \
      iptables ipset dnsutils ca-certificates curl git bash \
  && rm -rf /var/lib/apt/lists/*

# The Claude Code CLI — this is what carries your subscription auth.
RUN npm install -g @anthropic-ai/claude-code

# n8n-MCP: gives Claude REAL n8n node schemas + validation so it stops
# hallucinating node configs (the #1 problem everyone hits). Baked in so it
# works even with npm blocked at runtime by the firewall.
RUN npm install -g n8n-mcp

WORKDIR /app
COPY server.js init-firewall.sh entrypoint.sh package.json .mcp.json ask-human-mcp.js ./

# Files may be authored on Windows (CRLF); strip carriage returns so the
# shell scripts don't die on `#!/usr/bin/env bash\r`. Then make them runnable.
RUN sed -i 's/\r$//' /app/init-firewall.sh /app/entrypoint.sh \
  && chmod +x /app/init-firewall.sh /app/entrypoint.sh \
  && mkdir -p /home/node/.claude /workspace /worktrees \
  && chown -R node:node /home/node /app /workspace /worktrees

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
