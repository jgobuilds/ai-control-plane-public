# Build vs adopt: PII and secret scanning

**Date:** 2026-07-22 · **Lens:** `ai-standards/references/build-vs-adopt.md`

> **This record exists because the lens was skipped.** A PII scanner was
> hand-written first and surveyed afterwards. PII detection is squarely in the
> "solved problem domain" list the lens names. The survey below is what should
> have happened before a line was written — recorded honestly rather than
> quietly corrected.

All maturity and license figures are from the GitHub API, not rendered pages.

## Considered

| Candidate | Cost | Detects | License | State | Verdict |
| --- | --- | --- | --- | --- | --- |
| **gitleaks** | free — OSS | secrets | MIT | 28.2k, pushed daily | **Adopt** — with mandatory `--redact` |
| **Presidio** (`data-privacy-stack/presidio`) | free — OSS, self-hosted | PII | MIT | 10.1k, pushed daily | **Adopt** — structured entities + NER |
| **git-filter-repo** | free — OSS | remediation | effectively MIT | 12.8k, active | **Adopt** — history rewrite |
| detect-secrets (Yelp) | free — OSS | secrets | Apache-2.0 | 4.6k, **release stale since 2024-05** | Defer — good baseline model, pin a SHA if adopted |
| trufflehog | free OSS core | secrets | **AGPL-3.0** | 27.2k, active | Reject — leaks by default; AGPL fine internally but no advantage over gitleaks |
| ggshield | free tier; **metered above it** (SaaS) | secrets | MIT *client* | active | **Reject — cloud-gated.** Detector is GitGuardian's proprietary backend; every scan uploads file contents |
| scrubadub | free — OSS | PII | — | **dead: last push 2023-09**, open NLTK Zip-Slip vuln unanswered 5 months | Reject |
| piicatcher | free — OSS | PII | — | **archived** | Reject |
| piianalyzer | unpriced — commercial | PII | — | dead since 2015, 10 stars | Reject |
| Semgrep / CodeQL | OSS free; CodeQL free for public repos, **paid (GHAS) for private** | vulns, secrets | — | active | Reject for PII — no meaningful ruleset. CodeQL **removed** hardcoded-secret detection 2025-05-30 |
| BFG Repo-Cleaner | free — OSS | remediation | GPL-3.0 | slower cadence, JVM | Defer — simpler UX, less flexible |

## Chose

Replace the hand-written **detection engine**; keep the **orchestration**.

| Layer | Decision |
|---|---|
| Secrets detection | **gitleaks**, `--redact` pinned |
| Structured PII (SSN, card, IBAN, national IDs) | **Presidio** — checksummed, stronger than our regex |
| Names | **Presidio** NER |
| **Street addresses** | **keep ours** — see below |
| Placeholder allowlist | keep ours |
| Multi-mode staged / tree / history | keep ours |
| Never-print-matched-values | **keep ours — and wrap the adopted tools in it** |
| Policy layer (P0–P3 × visibility) | keep ours |
| History remediation | **git-filter-repo** |

The result is a thin orchestrator over gitleaks + Presidio, not a from-scratch
detector. Materially less to maintain.

## Because

**The one thing I got right is the thing the market gets wrong.** `gitleaks`
and `trufflehog` **print plaintext secrets to stdout by default.** Verified from
gitleaks' own flag registration:

```go
rootCmd.PersistentFlags().Uint("redact", 0, "...")
```

Default `0` — redaction **off** unless explicitly requested. Trufflehog is worse
by design: it needs the real value to verify a secret is live, so raw values sit
in its JSON output. CI logs are frequently more widely readable than the repo
and outlive the fix. So `--redact` is a **pinned config, not an option**, and
trufflehog's output would need a wrapper before touching a log.

Our never-print discipline is therefore not redundant with the adopted tools —
it is a property they lack and we must impose on them.

**Presidio has no street-address entity.** Its `LOCATION` is generic geo NER
(city, country, landmark), not a street parser. The real incident here was an
address in a docstring — the exact case that motivated our address regex.
Adopting Presidio does **not** retire that recognizer; nothing off-the-shelf
covers it.

**Nothing does the policy layer.** No OSS project maps data class × repo
visibility to a required mechanism. OPA/Rego could express it, but that is a
re-implementation, not an adoption.

**Presidio changed hands.** `microsoft/presidio` now 301-redirects to
`data-privacy-stack/presidio` — spun out, MIT retained, more active than before.
Our `CONTEXT-ARCHITECTURE.md` and `DETECTION-BACKENDS.md` both named the old
path; corrected in the same commit as this record.

**Rejections worth keeping.** `ggshield` is an MIT CLI wrapping a proprietary
cloud detector — every scan uploads file contents. For a shop scanning private
repos for PII, that inverts the goal. `scrubadub` looked alive in a search
summary and is nearly three years stale with an unpatched dependency
vulnerability. `CodeQL` deliberately exited secret detection in 2025.

## What this says about the process

The lens was written and then not applied. Two of the three components I built
(policy layer, no-leak discipline) turned out genuinely bespoke, and one
(the regex engine) duplicated mature tools. So the build was ~40% justified —
which is exactly the kind of result a twenty-minute survey surfaces before the
code exists rather than after.

The habit is the control. Skipping it worked twice earlier today and produced
avoidable rework here.

## Revisit if

- gitleaks changes its redaction default, or ships a values-never-printed mode
  (would simplify our wrapper).
- Presidio adds a street-address recognizer — then our last detection code goes.
- detect-secrets resumes releases — its hash-only baseline is the closest match
  to our design principle and would be worth layering for a legacy ledger.
- Any repo moves to a hosted CI where log retention is outside our control —
  raises the stakes on redaction from important to critical.

---

## Revision 2026-07-22 — four options the survey missed or mis-weighted

Raised after the fact. All verified via the GitHub API and vendor docs.

### The gap: I surveyed OSS and skipped the platform control

**GitHub Secret Scanning is free on public repositories** and needs no
maintenance. For our one public repo that makes it the highest-leverage control
available, and the survey never considered it because the brief said
"open source". Two capabilities no local tool can match:

- **Server-side enforcement.** A local hook is bypassable with `--no-verify` —
  the exact gap that forced "CI is the real gate". Push protection moves the
  check to a place the committer does not control.
- **Partner revocation.** On detecting a partner secret GitHub notifies the
  *issuer*, who can revoke it. Remediation without touching our infrastructure.

Limits: **credentials only, no PII**, and on **private** repos it requires
GitHub Secret Protection / Advanced Security on Team or Enterprise Cloud —
paid. Our fleet is 1 public + 10 private, so this covers exactly one repo for
free. It complements the local scanner; it does not replace it.

### Betterleaks — real, and the authorship claim checks out

`betterleaks/betterleaks`, MIT, 1,505 stars, pushed today, created 2026-02-03.
**`zricethezav` is the top contributor to both gitleaks (671 commits) and
betterleaks (219)**, and `rgmz` is second on both — the gitleaks maintainers
are building it.

That matters: adopting gitleaks means adopting the tool its own author has
moved past. But two cautions:

- **The "drop-in replacement, same CLI flags" claim is not in the README.** It
  came from a third-party summary. Unverified — do not plan a migration on it.
- **Redaction default could not be verified.** That is the criterion that
  decided against gitleaks, so it is a blocking unknown, not a detail.

At 5.5 months and 67 open issues it has not yet proven longevity against
gitleaks' 28k-star ecosystem. **Decision: gitleaks now, betterleaks as a dated
revisit** — not a dismissal.

### TruffleHog — right tool, wrong job

Its live verification against 800+ credential types is a genuine capability
neither gitleaks nor our scanner has, and it collapses false positives. But
verification requires the plaintext and an outbound call to the credential's
issuer.

That is wrong for a **commit-time gate** and right for **incident response**:
when a history scan surfaces a candidate, "is this still live?" is the question
that decides whether to rotate. Its leak-by-default output and AGPL matter far
less on a one-off triage run than in continuous scanning.

Notably **betterleaks also ships async validation**, so it may subsume both
roles — another reason to track it.

### detect-secrets — for onboarding, not steady state

Its hash-only baseline is the closest match to our never-print rule and is the
right tool for adopting a repo with many pre-existing findings: accept the
current state without storing plaintext. Releases remain stale; pin a SHA.

### Revised layering

| Layer | Tool | Why |
|---|---|---|
| Public repo, server-side | **GitHub Secret Scanning + push protection** | free, unbypassable, partner revocation |
| Commit-time, all repos | **gitleaks** (`--redact` pinned) + our PII regexes | fast, offline, no plaintext egress |
| CI | same, plus **Presidio** NER | model load is tolerable here, not in a hook |
| Incident response | **TruffleHog** | is this credential still live? |
| Legacy onboarding | **detect-secrets** baseline | hash-only accepted-findings ledger |
| History remediation | **git-filter-repo** | plus mandatory rotation |

### Added revisit triggers

- **Betterleaks reaches ~12 months with steady releases** and its redaction
  default is confirmed safe — re-evaluate as the gitleaks replacement.
- **GitHub Secret Protection pricing** is worth costing for the 10 private
  repos; server-side beats local enforcement wherever it is affordable.
