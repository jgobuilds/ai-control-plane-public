# Trusted artifact sources: an approval process now, a registry when it earns it

**Date:** 2026-07-25 · **Lens:** `ai-standards/references/dependency-security.md`,
`ai-standards/references/build-vs-adopt.md`, `ai-standards/references/cost-awareness.md`

## Considered

The requirement: pull packages and images only from a source that has been
**security-scanned and approved**, and check **at least daily** for
vulnerabilities in everything the framework and its agents use.

Facts verified 2026-07 (see *Sources*), not asserted from memory.

| Option | Cost | Verdict |
|---|---|---|
| **Approval process over a pinned allowlist** — `image-policy.json` as the register of approved artifacts, scan-before-add, CI gate | **$0** and no new infrastructure; small ongoing review toil (monthly) | **Adopt now** — we already had 80% of it; the missing piece was a documented entry gate |
| **Dependabot** — daily alerts + fix PRs, all ecosystems | **$0**, free on private repos, **zero infrastructure** | **Adopt now** — it was *disabled on all four repos*; free daily alerting simply switched off |
| **Trivy daily** (already running weekly) | $0; ~5 CI min/day, well inside the free tier | **Adopt now** — weekly is too slow against 36-hour exploitation |
| **Renovate** — stronger digest-bump automation | **Free for OSS, PAID for private repos beyond a limit**; 3 of our 4 repos are private | **Reject for now** — Dependabot covers the need at $0. Revisit if digest bumping becomes the bottleneck; that is Renovate's clear strength |
| **OSV-Scanner** — Google, free, 19+ lockfile formats | $0 | **Reject as redundant** — Dependabot already covers our pip/npm/docker/actions surface. Adopt only if we need SARIF in the Security tab |
| **Pull-through cache / mirror** (registry:2, Zot) | $0 licence; **~1 container** to run and patch | **Defer — the next rung.** The one thing digest pinning genuinely does not give us: insulation from upstream *deletion* |
| **Harbor** — CNCF graduated, Apache-2.0, scan-on-push, "prevent vulnerable images from running", Cosign content trust | **$0 licence, no commercial tiers** — but **6–9 containers** (Postgres, Redis, core, jobservice, registry, portal), TLS/certs, backups, and its own patch burden. A registry is itself an attack surface | **Defer** — the right end state, wrong size today |

## Chose

1. **Now — the process, not the infrastructure.** `context/image-policy.json` is
   the register of approved artifacts. Nothing enters the stack without: a scan
   at CRITICAL, a recorded digest, a written risk note, and a review date.
   `scripts/image_policy_check.py` enforces it in CI.
2. **Now — daily checking, fleet-wide.** Dependabot alerts + security updates
   **enabled on all four repos** (they were off), with `dependabot.yml` set to
   `daily` for every ecosystem in each repo. Trivy image scan moved weekly → daily.
3. **Defer the registry**, with explicit triggers below.

## Because

**Harbor is the right answer to the wrong-sized problem.** It does exactly what
was asked — scan-on-push, block pulls above a severity threshold, Cosign-enforced
content trust — and it is genuinely free. But it is 6–9 containers to run, patch
and back up, in service of a 7-container stack maintained by one person. Per
`cost-awareness.md`, the $0 licence is not the cost; the operational surface is,
and a registry that is itself unpatched is a net loss.

**Digest pinning already delivers most of a curated registry's value.** It gives
reproducibility and tamper-resistance (a mutated artifact fails the digest check).
What it does *not* give is **insulation from upstream deletion** — if a tag or
image is removed, we cannot pull it. That single gap is what the pull-through
cache rung addresses, at one container instead of nine.

**The largest real finding was free and switched off.** Dependabot alerts were
**disabled on every repo**. No amount of registry engineering compensates for not
turning on the free daily alerting that the platform already provides. Baseline
after enabling: **0 open alerts fleet-wide.**

## Status — and the triggers to revisit

Implemented: approval register + CI gate, Dependabot daily on 4 repos, Trivy daily.

**Adopt the pull-through cache when** any of: an upstream image we depend on is
deleted or retagged; we need to build with no internet; or we run more than ~10
distinct third-party images.

**Adopt Harbor when** any of: a second person needs to publish artifacts; a
customer or auditor requires provenance/signing attestations; we need
scan-on-push enforcement rather than scan-in-CI; or an air-gapped deployment is
required. Until one of those is true, Harbor is infrastructure we would be
maintaining for its own sake.

## Sources

[Harbor (CNCF, Apache-2.0, scan-on-push, prevent-vulnerable-pull, Cosign)](https://github.com/goharbor/harbor) ·
[Harbor signing docs](https://goharbor.io/docs/2.13.0/working-with-projects/working-with-images/sign-images/) ·
[Renovate Docker digest pinning](https://docs.renovatebot.com/docker/) ·
[Renovate vs Dependabot, incl. private-repo pricing](https://devopsboys.com/blog/renovate-vs-dependabot-dependency-updates-2026) ·
[OSV-Scanner](https://google.github.io/osv-scanner/github-action/)
