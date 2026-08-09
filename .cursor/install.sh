#!/usr/bin/env bash
# Idempotent Cloud Agent install: Python deps for Kalshi bots.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="${HOME}/.local/bin:${PATH}"

echo "==> Installing kalshi-bot dependencies"
if python3 -m venv .venv 2>/dev/null; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
  pip install -q -e ".[dev]"
else
  pip install --user -q -e ".[dev]"
fi

echo "==> Install complete"
