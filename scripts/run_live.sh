#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="${HOME}/.local/bin:${PATH}"

bash scripts/bootstrap_env.sh

if [[ -z "${KALSHI_API_KEY_ID:-}" ]]; then
  echo "ERROR: KALSHI_API_KEY_ID is not set. Add Kalshi API credentials to environment secrets."
  exit 1
fi
if [[ ! -f secrets/kalshi_private.key ]]; then
  echo "ERROR: secrets/kalshi_private.key missing. Set KALSHI_PRIVATE_KEY in environment secrets."
  exit 1
fi
if [[ -z "${CF_BENCHMARK_URL:-}" ]]; then
  echo "ERROR: CF_BENCHMARK_URL is not set. Official BRTI is required for live entries."
  exit 1
fi

exec python3 -m kalshi_bot --live --config config/live.yaml "$@"
