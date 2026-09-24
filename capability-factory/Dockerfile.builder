# Privileged build lane. Heavier than the runtime image (Go toolchain + Node +
# Claude Code + cli-printing-press). Used on-demand to GENERATE integrations,
# never to run production traffic.
# Node 22 is taken from the SAME pinned image the runtime services use, rather
# than from apt or a NodeSource install script. Reasons, in order:
#   1. bookworm's apt nodejs is Node 18, and @anthropic-ai/claude-code requires
#      >=22 — it installed with EBADENGINE warnings and then died at runtime on
#      `styleText` (a Node 22 API). That is what broke this build.
#   2. A `curl | bash` NodeSource installer would add an unpinned external script
#      to the highest-privilege image in the stack — exactly what
#      ai-standards/references/dependency-security.md says not to do.
#   3. Reusing the existing pin means one fewer artifact to track and review.
# node:22-bookworm-slim — re-pinned 2026-09-17, see context/image-policy.json
FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS node22

# golang:1.26-bookworm — pinned 2026-09-03 (tag named 2026-09-17), see context/image-policy.json
FROM golang:1.26-bookworm@sha256:e8c859f5632dcfde7b32d2012b4351728f6437930887c2f6a91ea242459e5514
# Both stages are bookworm/glibc, so the Node runtime copies across cleanly.
COPY --from=node22 /usr/local/bin/node /usr/local/bin/node
COPY --from=node22 /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
# Tooling printing-press uses (node now comes from the stage above, not apt).
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl git ca-certificates jq sqlite3 \
  && rm -rf /var/lib/apt/lists/*

# Claude Code (printing-press drives it) — PINNED to an exact version.
#
# Unpinned, every build took whatever `latest` was that day: 2.1.274 on
# 2026-09-17, while the production runners were on 2.1.220. An exact npm
# version is a real pin here, not a nominal one — checked 2026-09-17 against
# the published package: install.cjs downloads nothing, the native binary
# arrives through optionalDependencies locked to the SAME version
# (@anthropic-ai/claude-code-linux-x64@<version>), and npm verifies every
# tarball's integrity against the registry.
#
# WHY 2.1.267: it is Anthropic's `stable` dist-tag on that date. It is past the
# fix floor of every published advisory for this package (the most recent,
# GHSA-7835-87q9-rgvv / GHSA-fg94-h982-f3mm, fixed 2.1.163). It is close to what
# printing-press v4.32.2 was last run with, where dropping to 2.1.220 would be
# seven weeks of CLI behaviour the generator was never exercised against.
#
# The version assertion is the proof the pin took effect. The native binary is
# only put in place by the postinstall step; if that does not run (install
# scripts disabled, or a failed platform-package fetch), bin/claude.exe stays a
# 500-byte placeholder that prints "Error: claude native binary not installed"
# and the image would ship without a working claude. Watched failing 2026-09-17
# with --ignore-scripts: the assertion fires and the build exits 1. (Not
# --omit=optional: npm ignores that for global installs and fetches the platform
# package anyway, which was the first thing this was wrongly tested with.)
#
# REVISIT at the monthly image-policy review (context/image-policy.json
# reviewBy): move to the then-current `stable` after checking the advisory list
# (`gh api "advisories?ecosystem=npm&affects=@anthropic-ai/claude-code"`).
ARG CLAUDE_CODE_VERSION=2.1.267
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
  && claude --version | grep -q "^${CLAUDE_CODE_VERSION} " \
  || { echo "claude-code is not ${CLAUDE_CODE_VERSION} after install: $(claude --version 2>&1)"; exit 1; }

# cli-printing-press — built from a PINNED commit, with one dependency overridden.
#
# This was `curl -fsSL .../cli-printing-press/main/scripts/install.sh | bash`: an
# unpinned script from a personal repo's main branch, run as root, in the
# highest-privilege image here — the exact pattern reason 2 above rejects for
# Node. It ran `go install ...@latest` and `npx skills@latest`, so no two builds
# were guaranteed to contain the same code.
#
# THE OVERRIDE. Every upstream release through v4.32.2 (and main, on 2026-09-17)
# pins github.com/getkin/kin-openapi v0.137.0, which carries four advisories:
#   GHSA-r277-6w6q-xmqw  CRITICAL  ValidationHandler.Load() fail-open auth bypass   fixed 0.144.0
#   GHSA-jpcw-4wr7-c3vq  medium    openapi3filter nil-pointer panic                 fixed 0.144.0
#   GHSA-xhj3-7xw9-vr34  high      openapi3filter uncontrolled resource consumption fixed 0.142.0
#   GHSA-mmfr-pmjx-hw9w  high      ConvertErrors nil-pointer panic                  fixed 0.141.0
# The scan gate is CRITICAL-only, so it reported one; the other three were real
# too. v0.144.0 is the lowest version that fixes all four. Upgrading
# printing-press does not help — upstream has not bumped it — so we bump it here.
# REVISIT: when an upstream release pins kin-openapi >= 0.144.0, drop the
# `go get` line and move PRINTING_PRESS_REF/SHA to that release.
#
# The SHA check is the pin: a tag can be moved, a commit cannot. The version
# ldflag matches upstream's goreleaser config, so the binary still reports its
# release version instead of a devel fallback.
ARG PRINTING_PRESS_REF=v4.32.2
ARG PRINTING_PRESS_SHA=d4f4b3a9a27c006f5897ec003e27d847b754c8c9
ARG KIN_OPENAPI=v0.144.0
RUN git clone --depth 1 --branch "$PRINTING_PRESS_REF" https://github.com/mvanhorn/cli-printing-press /tmp/cpp \
  && test "$(git -C /tmp/cpp rev-parse HEAD)" = "$PRINTING_PRESS_SHA" \
  && cd /tmp/cpp \
  && go get "github.com/getkin/kin-openapi@$KIN_OPENAPI" \
  && go mod tidy \
  && CGO_ENABLED=0 go build -trimpath \
       -ldflags "-s -w -X github.com/mvanhorn/cli-printing-press/v4/internal/version.Version=${PRINTING_PRESS_REF#v}" \
       -o /go/bin/cli-printing-press ./cmd/cli-printing-press \
  && mkdir -p /opt/printing-press \
  && cp -r skills /opt/printing-press/skills \
  && cd / && rm -rf /tmp/cpp

# node user for parity with the runtime image; give it a home for ~/.claude.
# ORDER MATTERS: `node` is created FIRST so it can take uid 1000. Creating
# `builder` first claimed 1000, and the golang base (unlike the node base) has no
# pre-existing node user, so `useradd -u 1000 node` then died with "UID 1000 is
# not unique" and failed the build. That was masked for as long as the Node 18
# npm step above failed first — one broken step hiding the next.
#
# The printing-press skills go to the user that RUNS Claude Code. The old
# installer ran as root, so they landed in /root/.claude/skills while this image
# runs as node with HOME=/home/node — the /printing-press commands described
# below were never visible. They are copied verbatim from the pinned commit,
# which is what the installer did, minus its unpinned `npx skills@latest`.
RUN (id node >/dev/null 2>&1 || useradd -m -u 1000 node) \
  && (useradd -m -s /bin/bash builder 2>/dev/null || true) \
  && mkdir -p /home/node/.claude/skills /generated /workspace \
  && cp -r /opt/printing-press/skills/. /home/node/.claude/skills/ \
  && chown -R node:node /home/node /generated /workspace

WORKDIR /generated
USER node
ENV HOME=/home/node

# Drop into a shell; run printing-press slash commands from inside Claude Code:
#   claude
#   > /printing-press <api-name-or-url>
CMD ["bash"]
