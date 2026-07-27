# The python lane-runner. n8n cannot execute python (hardened image, no package
# manager — see docs/decisions/0006), so n8n calls this over the internal network
# instead. Nothing but stdlib, so no pip install and no dependency surface.
#
# The repo's scripts/ and the audit ledger are BIND-MOUNTED read-only at runtime
# (see docker-compose.yml) — never baked in, so the image carries no code or data.
# python:3.12-alpine — pinned 2026-07-24, see context/image-policy.json
FROM python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
WORKDIR /srv
COPY server.py /srv/server.py
# Drop privileges — this service only ever reads mounted files.
RUN adduser -D -H lanes
USER lanes
EXPOSE 8081
CMD ["python", "/srv/server.py"]
