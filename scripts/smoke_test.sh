#!/usr/bin/env bash
#
# Functional smoke test — end-to-end against the LIVE docker stack.
#
# This is the ONE test that exercises the real router over HTTP: health, a
# low-risk deterministic route, the PII-block refusal, and the tamper-evident
# audit ledger. It is NOT run in the node-free/docker-free dev sandbox — run it
# on a host with docker + the stack built.
#
# ─────────────────────────────────────────────────────────────────────────────
# REQUIREMENTS
#   * docker + docker compose, stack built (`docker compose build`).
#   * AUTH: either export a real ROUTER_TOKEN (matching .env), OR run the router
#     with ALLOW_NO_AUTH=1 (LOCAL DEV ONLY — never in production). If ROUTER_TOKEN
#     is set here it is sent as the x-router-token header; otherwise we assume the
#     router was booted with ALLOW_NO_AUTH=1.
#   * The router publishes NO host port (by design), so we reach it from a
#     throwaway curl container attached to the compose network. Needs to pull
#     curlimages/curl once (internet).
#
# USAGE
#   ROUTER_TOKEN=xxx bash scripts/smoke_test.sh
#   # or, dev: bring the stack up with ALLOW_NO_AUTH=1 then: bash scripts/smoke_test.sh
#
# It FAILS LOUDLY: any unexpected status or body aborts with a red FAIL line and a
# non-zero exit. Green means the governance chokepoint is live and enforcing.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1" >&2; exit 1; }

TOKEN_HEADER=()
if [ -n "${ROUTER_TOKEN:-}" ]; then
  TOKEN_HEADER=(-H "x-router-token: ${ROUTER_TOKEN}")
  echo "auth: sending x-router-token header"
else
  echo "auth: no ROUTER_TOKEN set — assuming the router was booted with ALLOW_NO_AUTH=1 (dev only)"
fi

# 0. Bring the core stack up (idempotent). Runners/scrubber/router + n8n.
echo "== bringing up the stack =="
docker compose up -d router claude-runner gemini-runner scrubber >/dev/null 2>&1 \
  || fail "docker compose up failed — is the stack built? (docker compose build)"

# Resolve the compose network so a throwaway curl container can reach router:8080.
NET="$(docker network ls --format '{{.Name}}' | grep -E 'agentnet' | head -1)"
[ -n "$NET" ] || fail "could not find the agentnet docker network"

# curl helper: run curl INSIDE the compose network (router has no host port).
rcurl() { docker run --rm --network "$NET" curlimages/curl:latest -s "$@"; }

# Give the router a moment to bind.
for i in $(seq 1 30); do
  if rcurl -o /dev/null -w '%{http_code}' http://router:8080/health 2>/dev/null | grep -q 200; then
    break
  fi
  [ "$i" -eq 30 ] && fail "router /health never returned 200 (check: docker compose logs router)"
  sleep 1
done

# 1. Health.
code="$(rcurl -o /dev/null -w '%{http_code}' http://router:8080/health)"
[ "$code" = "200" ] && pass "router /health -> 200" || fail "health returned $code"

# 2. Low-risk deterministic route: "ping" hits the healthcheck rule (Tier 0, no
#    model call). Expect 200 + a decision that is deterministic with tier t0.
low="$(rcurl -X POST http://router:8080/route \
  -H 'content-type: application/json' "${TOKEN_HEADER[@]}" \
  -d '{"prompt":"ping","action":"advise","trigger":"turn"}')"
echo "$low" | grep -q '"deterministic":true' && echo "$low" | grep -q '"tier":"t0"' \
  && pass "low-risk /route -> 200 deterministic decision (tier t0)" \
  || fail "low-risk route missing deterministic t0 decision: $low"

# 3. PII-block: post a RAW email to a pii:block scope. The router MUST refuse to
#    forward raw PII (default scope 'enterprise' is pii:block). Expect a refusal
#    (error mentions PII) — the raw value never reaches a runner. (A tokenized
#    ingest flow is the sanctioned alternative; either way, raw PII is blocked.)
pii="$(rcurl -X POST http://router:8080/route \
  -H 'content-type: application/json' "${TOKEN_HEADER[@]}" \
  -d '{"prompt":"please email jane.doe@example.com the report","scope":"enterprise","action":"advise","trigger":"turn"}' || true)"
echo "$pii" | grep -qi 'PII' \
  && pass "pii:block scope refuses raw email (blocked/tokenized, not forwarded)" \
  || fail "expected a PII refusal for a raw email in a pii:block scope: $pii"

# 4. Audit ledger: the decisions above must have been appended, and the
#    tamper-evident chain must verify. ./audit is bind-mounted from the router.
[ -f audit/decisions.jsonl ] || fail "no audit/decisions.jsonl was written"
lines="$(wc -l < audit/decisions.jsonl | tr -d ' ')"
[ "$lines" -ge 1 ] && pass "audit ledger appended ($lines record(s))" || fail "audit ledger empty"

python scripts/verify_audit.py audit/decisions.jsonl \
  && pass "verify_audit.py: hash chain intact" \
  || fail "verify_audit.py reported tampering on a freshly-written ledger"

echo
echo "SMOKE TEST PASSED — router is live and enforcing (health, deterministic route, PII block, audit chain)."
