# node:22-bookworm-slim — pinned 2026-07-24, see context/image-policy.json
FROM node@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3
WORKDIR /app
# Copy the whole router source so all modules are included (server.js requires
# ./rbac.js, ./killswitch.js, ./guardrails.js in addition to policy.json/rules.js).
COPY . ./
# Pure-Node, no deps. Runs on the internal network only (no host port, no egress
# needed beyond reaching the runner containers).
USER node
EXPOSE 8080
CMD ["node", "/app/server.js"]
