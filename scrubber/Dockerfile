# node:22-bookworm-slim — re-pinned 2026-09-17, see context/image-policy.json
FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5
WORKDIR /app
COPY server.js package.json ./
# Runs as root intentionally: it must write workspace dirs owned by various
# uids across bind mounts. Internal network only; no egress needed at all.
EXPOSE 8080
CMD ["node", "/app/server.js"]
