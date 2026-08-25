#!/usr/bin/env bash
# Continuous 15-minute KXBTC15M BRTI websocket bot with auto-restart.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="${HOME}/.local/bin:${PATH}"

bash scripts/bootstrap_env.sh
if [[ -f .env ]]; then
  set -a
  # shellcheck source=/dev/null
  source .env
  set +a
fi
if [[ -f .venv/bin/activate ]]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

CONFIG="${CONFIG_BRTI_15M:-config/brti_15m.yaml}"
mkdir -p logs

effective_dry_run() {
  echo "${DRY_RUN_15M:-${DRY_RUN:-true}}"
}

live_flag() {
  if [[ "$(effective_dry_run)" == "false" && -n "${KALSHI_API_KEY_ID:-}" && -f secrets/kalshi_private.key ]]; then
    LIVE_FLAG=(--live)
  else
    LIVE_FLAG=()
  fi
}

live_flag

while true; do
  if [[ -f .env ]]; then
    set -a
    # shellcheck source=/dev/null
    source .env
    set +a
  fi
  live_flag
  echo "[$(date -Is)] starting brti-ws-bot mode=${LIVE_FLAG[*]:-PAPER}"
  PYTHONPATH=src python3 -m kalshi_bot "${LIVE_FLAG[@]}" --brti-ws --config "$CONFIG" \
    2>&1 | tee -a logs/brti_15m.log || true
  echo "[$(date -Is)] brti-ws-bot exited; restarting in 5s"
  sleep 5
done
