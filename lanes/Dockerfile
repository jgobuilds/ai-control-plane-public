# The python lane-runner. n8n cannot execute python (hardened image, no package
# manager — see docs/decisions/0006), so n8n calls this over the internal network
# instead.
#
# ONE dependency, and it used to be zero. PyYAML was added so /recurrence can read
# reliability/recurrence.yaml; without it the endpoint returned every signature as
# undispositioned. The reasoning and the three rejected alternatives are in
# requirements.txt, next to the pin. Keep the count at one: this service reads
# mounted files and answers JSON, and every package added here runs beside the
# audit ledger.
#
# The repo's scripts/ and the audit ledger are BIND-MOUNTED read-only at runtime
# (see docker-compose.yml) — never baked in, so the image carries no code or data.
# python:3.12-alpine — re-pinned 2026-09-17, see context/image-policy.json
FROM python:3.12-alpine@sha256:b64631e04e4920160c50fbe8d8df828f7f35f06f425cb44aa09bca53e708a35a
WORKDIR /srv
# Before server.py, so editing the server does not re-resolve the dependency.
COPY requirements.txt /srv/requirements.txt
RUN python -m pip install --no-cache-dir -r /srv/requirements.txt
COPY server.py /srv/server.py
# Drop privileges — this service only ever reads mounted files.
RUN adduser -D -H lanes
USER lanes
EXPOSE 8081
CMD ["python", "/srv/server.py"]
