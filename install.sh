#!/usr/bin/env bash
# the control plane installer (wrapper) — see scripts/install.py for options.
#   ./install.sh            interactive
#   ./install.sh --up       accept defaults, then build + start
set -euo pipefail
cd "$(dirname "$0")"
exec "$(command -v python3 || command -v python)" scripts/install.py "$@"
