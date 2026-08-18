#!/usr/bin/env bash
# Materialize Cloud Agent secrets into local paths the bot expects.
# Safe to run on every boot; refreshes credentials when environment secrets change.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p secrets data logs

if [[ -n "${KALSHI_PRIVATE_KEY:-}" ]]; then
  printf '%s\n' "$KALSHI_PRIVATE_KEY" > secrets/kalshi_private.key
  chmod 600 secrets/kalshi_private.key
fi

existing_api_key=""
if [[ -f .env ]]; then
  existing_api_key="$(grep -E '^KALSHI_API_KEY_ID=' .env | head -n1 | cut -d= -f2- || true)"
fi

api_key_id="${KALSHI_API_KEY_ID:-$existing_api_key}"

has_creds=false
if [[ -n "$api_key_id" && -f secrets/kalshi_private.key ]]; then
  has_creds=true
fi

default_dry_run="${DRY_RUN:-true}"
default_dry_run_15m="${DRY_RUN_15M:-$default_dry_run}"
default_dry_run_1h="${DRY_RUN_1H:-$default_dry_run}"
default_benchmark="${BENCHMARK_MODE:-constituent_proxy}"
if [[ "$has_creds" == true ]]; then
  default_benchmark="${BENCHMARK_MODE:-kalshi_passthrough}"
fi

cat > .env <<EOF
KALSHI_API_KEY_ID=${api_key_id}
KALSHI_PRIVATE_KEY_PATH=./secrets/kalshi_private.key
KALSHI_ENV=${KALSHI_ENV:-prod}
CF_BENCHMARK_URL=${CF_BENCHMARK_URL:-kalshi://BRTI}
CF_BENCHMARK_API_KEY=${CF_BENCHMARK_API_KEY:-}
CF_BENCHMARK_API_KEY_HEADER=${CF_BENCHMARK_API_KEY_HEADER:-Authorization}
CF_BENCHMARK_API_KEY_PREFIX=${CF_BENCHMARK_API_KEY_PREFIX:-Bearer}
BENCHMARK_MODE=${default_benchmark}
DRY_RUN=${default_dry_run}
DRY_RUN_15M=${default_dry_run_15m}
DRY_RUN_1H=${default_dry_run_1h}
MAX_POSITION_USD=${MAX_POSITION_USD:-50}
MAX_DAILY_LOSS_USD=${MAX_DAILY_LOSS_USD:-100}
MIN_BOOK_DEPTH_USD=${MIN_BOOK_DEPTH_USD:-25}
POLL_INTERVAL_SEC=${POLL_INTERVAL_SEC:-1.0}
RISK_FREE_RATE=${RISK_FREE_RATE:-0.05}
EOF
