#!/bin/bash
# Dev-only: launch the manager in demo mode.
# No server, no worker, no real config changes — simulated dashboard states.
set -euo pipefail
cd "$(dirname "$0")/.."
exec venv-linux/bin/python app.py --demo "$@"
