#!/usr/bin/env bash
# Edge Desk web dashboard — all trades from 15m and 1h bots.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="${HOME}/.local/bin:${PATH}"

if [[ -f .venv/bin/activate ]]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

HOST="${DASHBOARD_HOST:-0.0.0.0}"
PORT="${DASHBOARD_PORT:-8787}"

exec python3 -m kalshi_bot --dashboard --host "$HOST" --port "$PORT"
