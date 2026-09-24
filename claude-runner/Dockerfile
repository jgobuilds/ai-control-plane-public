# node:22-bookworm-slim — re-pinned 2026-09-17, see context/image-policy.json
FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5
# iptables/ipset: egress firewall. dnsutils: dig (resolve allowlist). curl:
# healthcheck. git/bash: worktree-per-agent fan-out + self-verify commands.
RUN apt-get update && apt-get install -y --no-install-recommends \
      iptables ipset dnsutils ca-certificates curl git bash \
  && rm -rf /var/lib/apt/lists/*

# The agent CLIs, PINNED with the whole dependency tree locked (tools/):
#   - Claude Code — this is what carries your subscription auth.
#   - n8n-MCP — gives Claude REAL n8n node schemas + validation so it stops
#     hallucinating node configs (the #1 problem everyone hits). Baked in so it
#     works even with npm blocked at runtime by the firewall.
#
# These floated until 2026-09-17: `npm install -g <name>` with no version, so the
# CLI a rebuild shipped was whatever was newest that day. The stack was rebuilt
# that morning and production moved from Claude Code 2.1.220 to 2.1.274 without
# anyone choosing it — and the --tools / --disallowedTools enforcement this runner
# depends on had only been probed against 2.1.220.
#
# HOW THE VERSIONS ARE CHOSEN — a 7-day minimum release age. Each tool is pinned
# to the newest release at least 7 days old that is also past the fix floor of
# every published advisory for it. A week-old release has been run by everyone
# who upgraded on day one, so a regression it shipped has had a chance to be
# reported, patched or withdrawn before it reaches us. Security fixes skip the
# wait. Applied 2026-09-17; ages are from the npm registry that day:
#   @anthropic-ai/claude-code  2.1.267  7.7d  — also Anthropic's `stable` dist-tag;
#                                              advisories fixed by 2.1.163
#   n8n-mcp                    2.84.0   7.6d  — 8 advisories in four months, the
#                                              latest fixed by 2.57.4
#   @google/gemini-cli         0.59.0   8.6d  — critical RCE fixed by 0.39.1
# Dependabot proposes newer versions on the same rule (cooldown default-days: 7 in
# .github/dependabot.yml); its SECURITY updates are not delayed by a cooldown.
#
# WHY A LOCKFILE, NOT `npm install -g pkg@version`. A global install pins only the
# package named: n8n-mcp resolves 158 packages beneath it, all floating within
# their semver ranges on every build. `npm ci` against the committed lockfile
# installs exactly the tree that was reviewed, verifying every tarball's integrity
# hash, and fails if package.json and the lock disagree.
#
# WHY /opt/agent-tools, ROOT-OWNED. The agent runs as `node` with Bash, and /app is
# chowned to node below. Installing the CLIs there would let an agent rewrite its
# own binary; a root-owned prefix keeps the property the old global install had.
#
# The final checks are the proof the pins took effect: every installed top-level
# version must equal package.json, and the claude binary must report its pinned
# version — the native binary is placed by a postinstall step, and without it
# bin/claude.exe is a 500-byte placeholder that prints an error.
COPY tools/package.json tools/package-lock.json /opt/agent-tools/
RUN npm ci --prefix /opt/agent-tools --omit=dev --no-audit --no-fund \
  && ln -sf /opt/agent-tools/node_modules/.bin/claude /usr/local/bin/claude \
  && ln -sf /opt/agent-tools/node_modules/.bin/n8n-mcp /usr/local/bin/n8n-mcp \
  && node -e 'const d=require("/opt/agent-tools/package.json").dependencies;for(const[n,v]of Object.entries(d)){const g=require("/opt/agent-tools/node_modules/"+n+"/package.json").version;if(g!==v){console.error(n+" installed "+g+", pinned "+v);process.exit(1)}}' \
  && claude --version | grep -q "^$(node -p 'require("/opt/agent-tools/package.json").dependencies["@anthropic-ai/claude-code"]') "

WORKDIR /app
COPY server.js pii-patterns.js init-firewall.sh entrypoint.sh package.json .mcp.json ask-human-mcp.js ./

# Files may be authored on Windows (CRLF); strip carriage returns so the
# shell scripts don't die on `#!/usr/bin/env bash\r`. Then make them runnable.
RUN sed -i 's/\r$//' /app/init-firewall.sh /app/entrypoint.sh \
  && chmod +x /app/init-firewall.sh /app/entrypoint.sh \
  && mkdir -p /home/node/.claude /workspace /worktrees \
  && chown -R node:node /home/node /app /workspace /worktrees

EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
