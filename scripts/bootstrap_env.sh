#!/usr/bin/env bash
# Materialize Cloud Agent secrets into local paths the bot expects.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p secrets data logs

if [[ -n "${KALSHI_PRIVATE_KEY:-}" ]]; then
  printf '%s\n' "$KALSHI_PRIVATE_KEY" > secrets/kalshi_private.key
  chmod 600 secrets/kalshi_private.key
fi

if [[ ! -f .env ]]; then
  cat > .env <<EOF
KALSHI_API_KEY_ID=${KALSHI_API_KEY_ID:-}
KALSHI_PRIVATE_KEY_PATH=./secrets/kalshi_private.key
KALSHI_ENV=${KALSHI_ENV:-prod}
CF_BENCHMARK_URL=${CF_BENCHMARK_URL:-kalshi://BRTI}
CF_BENCHMARK_API_KEY=${CF_BENCHMARK_API_KEY:-}
CF_BENCHMARK_API_KEY_HEADER=${CF_BENCHMARK_API_KEY_HEADER:-Authorization}
CF_BENCHMARK_API_KEY_PREFIX=${CF_BENCHMARK_API_KEY_PREFIX:-Bearer}
BENCHMARK_MODE=${BENCHMARK_MODE:-constituent_proxy}
DRY_RUN=${DRY_RUN:-true}
MAX_POSITION_USD=${MAX_POSITION_USD:-50}
MAX_DAILY_LOSS_USD=${MAX_DAILY_LOSS_USD:-100}
MIN_BOOK_DEPTH_USD=${MIN_BOOK_DEPTH_USD:-25}
POLL_INTERVAL_SEC=${POLL_INTERVAL_SEC:-1.0}
RISK_FREE_RATE=${RISK_FREE_RATE:-0.05}
EOF
fi
