# node:22-bookworm-slim — pinned 2026-07-24, see context/image-policy.json
FROM node@sha256:6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3
WORKDIR /app
COPY server.js package.json ./
# Runs as root intentionally: it must write workspace dirs owned by various
# uids across bind mounts. Internal network only; no egress needed at all.
EXPOSE 8080
CMD ["node", "/app/server.js"]
