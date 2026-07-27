# node:22-bookworm-slim — pinned 2026-07-24, see context/image-policy.json
FROM node@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3
# iptables/ipset: egress firewall. dnsutils: dig. curl/git: CLI + auth flows.
RUN apt-get update && apt-get install -y --no-install-recommends \
      iptables ipset dnsutils ca-certificates curl git \
  && rm -rf /var/lib/apt/lists/*

# Provider-flagged: swap these two ARGs to move gemini -> antigravity (or any
# other headless agent CLI) without touching server.js.
ARG AGENT_NPM=@google/gemini-cli
ARG AGENT_BIN=gemini
RUN npm install -g "${AGENT_NPM}"
ENV AGENT_BIN=${AGENT_BIN}

WORKDIR /app
COPY server.js init-firewall.sh entrypoint.sh package.json ./

RUN sed -i 's/\r$//' /app/init-firewall.sh /app/entrypoint.sh \
  && chmod +x /app/init-firewall.sh /app/entrypoint.sh \
  && mkdir -p /home/node/.gemini /workspace \
  && chown -R node:node /home/node /app /workspace

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
