# Upgrades — when to move a pin, how to prove it, how to ship it

Every third-party piece this stack runs is pinned: base images by `tag@digest`, the
agent CLIs in the runners by lockfile, the builder's CLIs by exact version. Pinning
is half a control. Without a process for moving the pins it is worse than floating —
you freeze, deliberately, on whatever was current the day you pinned. This is that
process: two lanes for deciding *when*, one gate for proving a version, and one path
for shipping it.

## Why this exists

On 2026-09-17, in one day:

- a Dependabot base-image bump was accepted whose digests were `node:latest` and
  `python:latest`, because the `FROM` lines named no tag — Node 22→26 and slim→full
  images, with every check green;
- the runners installed their agent CLIs with no version, and an unpinned rebuild
  moved production Claude Code from 2.1.220 to 2.1.274 without anyone choosing it —
  while the tool-restriction flags the runner depends on had only been probed on
  2.1.220;
- the whole stack was then recreated at once, twice in sixteen minutes, with no step
  at which anything was checked.

Nothing broke. Nothing would have contained it if something had.

## Two lanes

| | Security fix | Version update |
|---|---|---|
| **Trigger** | A published advisory affecting a version we run | A newer release exists |
| **Wait** | None — triage reachability, then patch (days for critical) | **7-day minimum release age** |
| **Dependabot** | Security updates, never subject to cooldown | `cooldown: { default-days: 7 }` on every entry |
| **Ceiling** | — | Take an eligible upgrade within **30 days** |

Per GitHub's Dependabot options reference, "the cooldown option is only available for
version updates, not security updates" — so the wait never delays a fix.

**Why 7 days.** A week-old release has been run by everyone who upgraded on day one,
so a regression it shipped has had a chance to be reported, patched or withdrawn
before it reaches us. On 2026-09-17 the rule picked Claude Code 2.1.267 — which was
also Anthropic's own `stable` dist-tag. Prefer the vendor's stable channel when one
exists.

**Why a ceiling.** The floor stops moving too early; nothing else stops falling too
far behind, and a security fix then forces the whole jump at once. The ceiling
measures *neglect*, not age: from the day the first newer release cleared the
cooldown, how long it has gone untaken.

## What enforces each rule

| Rule | Enforced by | Runs |
|---|---|---|
| Every Dependabot entry has a ≥ 7-day cooldown | `scripts/dependabot_check.py` | CI (`quality`) |
| Every pinned image ref names its policy tag (`image:tag@sha256`) | `scripts/image_policy_check.py` | CI (`quality`) |
| Base-image pins reviewed monthly | `image_policy_check.py` (`reviewBy`) | CI (`quality`) |
| No npm pin left > 30 days past an eligible upgrade | `scripts/pin_staleness.py` | weekly + on pin changes (`pin-staleness`) |
| Runner CLIs install exactly the locked tree | `npm ci` + version assertions in the Dockerfiles | every image build |
| Tool restrictions still bite on a CLI build | `scripts/probe_cli_enforcement.py` | by hand, before promoting a CLI (needs credentials) |
| One runner first, verify, then the rest | `scripts/rollout.py` | every deploy |

## Moving a pin

**Runner agent CLIs** (`claude-runner/tools/`, `gemini-runner/tools/`):

1. Choose the version: the newest release at least 7 days old that is past every
   advisory's fix floor. `python scripts/pin_staleness.py` names the version policy
   allows today; `gh api "advisories?ecosystem=npm&affects=<package>"` lists
   advisories.
2. Edit `tools/package.json`, then regenerate the lock **inside the pinned node
   image**, so the npm that writes it is the npm that reads it:
   ```bash
   MSYS_NO_PATHCONV=1 docker run --rm -v "<abs path>/claude-runner/tools:/t" -w /t \
     node:22-bookworm-slim@<digest from the Dockerfile> \
     npm install --package-lock-only --ignore-scripts --no-audit --no-fund
   ```
3. Build the image and **probe it** (step below).

**Builder CLIs** — `ARG CLAUDE_CODE_VERSION` in `capability-factory/Dockerfile.builder`.

**Base images** — resolve the tag's current digest and **check what it is** before
accepting it (`docker buildx imagetools inspect <image:tag>`, then run it and read
`/etc/os-release` and the runtime version). Update `FROM image:tag@sha256:…` and
`context/image-policy.json` together. A digest pin is only as good as the claim about
what it pins.

## Proving a Claude Code version

```bash
docker compose build claude-runner-commons   # or build the image by hand
python scripts/probe_cli_enforcement.py --image <image> --token-from claude-runner-commons
```

Exit 0 is the only pass. It runs four sessions — Bash and MCP, each against a positive
control — and decides from the CLI's own `system/init` tool list, not the model's
words. Exit 2 (inconclusive: a control failed, a server did not connect) is not a
pass; fix the cause and run it again. It was watched returning 1 on the pre-fix
`--allowedTools` flag.

## Shipping it

Run from **the live checkout** — the directory the stack was started from. The tool
refuses anything else: Compose resolves `./audit` and `./workspace` against the
compose file, so a deploy from a worktree re-points production at that worktree's
ledger and scope tree.

```bash
git pull                                              # the change, committed, in the live checkout
python scripts/rollout.py plan                        # waves and what runs now
python scripts/rollout.py canary --probe              # one runner (claude-runner-commons), verified
# let real work reach it
python scripts/rollout.py observe                     # ledger since the canary started
python scripts/rollout.py promote                     # remaining waves, verify each, stop on failure
python scripts/rollout.py verify                      # anytime, from anywhere: read-only
python scripts/rollout.py rollback <service>          # previous image, if something is wrong
```

Waves go callees before callers: the canary, the other runners, the support services,
the router, then n8n. `observe` treats a canary no work has reached as **inconclusive**
— an idle canary proves nothing — and fails on any runner error the ledger attributes
to it. Every mutating command takes `--dry-run`.
