# node:22-bookworm-slim — re-pinned 2026-09-17, see context/image-policy.json
FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5
WORKDIR /app
# Copy the whole router source so all modules are included (server.js requires
# ./rbac.js, ./killswitch.js, ./guardrails.js in addition to policy.json/rules.js).
COPY . ./
# Pure-Node, no deps. Runs on the internal network only (no host port, no egress
# needed beyond reaching the runner containers).
USER node
EXPOSE 8080
CMD ["node", "/app/server.js"]
