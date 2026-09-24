---
id: SOL-20260727-lanes-server-baked
title: A new lanes endpoint 404'd for hours after it was written
status: solved
last_verified: 2026-07-27
tags: [n8n, operations]
signature: no such lane
---

# A new lanes endpoint 404'd for hours after it was written

## What happened

A `/dispatch` endpoint was added to `lanes/server.py` and verified on disk. Two scheduled runs then failed with `404 no such lane`, and a third failed after that. The endpoint was in the file the whole time.

## Why it happened

`./scripts` is bind-mounted into the container, so edits to `scripts/*.py` take effect immediately. **`server.py` is not** — it is baked into the image at `/srv/server.py`, and the running container was seven hours old.

The surprise is precisely that everything *beside* it behaves the other way. "I added the endpoint" and "the endpoint exists" diverged silently, and nothing in the failure pointed at staleness.

## What was tried that did NOT work

- **Checked the file on the host.** Correct, and irrelevant — the host is not what serves the request.
- **Restarted n8n.** Wrong container entirely; it changed nothing because n8n was never the problem.

## The fix

`docker compose up -d --build lanes`, then verify by calling the exact URL the caller uses, **from inside the calling container** — not from the host, which can reach a different thing.

## Prevention

A warning at the top of `lanes/server.py` itself, stating that editing it requires a rebuild. It is placed there rather than in a runbook because that is where someone will be when they need it.

## How we would know it came back

`docker exec lanes grep -c "<route>" /srv/server.py` — check the container, not the repo. A 404 for a route you can see in your editor is this, almost every time.
