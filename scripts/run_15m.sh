#!/usr/bin/env bash
# Continuous 15-minute KXBTC15M bot with auto-restart.
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

CONFIG="${CONFIG_15M:-config/default.yaml}"
mkdir -p logs

live_flag() {
  if [[ "${DRY_RUN:-true}" == "false" && -n "${KALSHI_API_KEY_ID:-}" && -f secrets/kalshi_private.key ]]; then
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
  echo "[$(date -Is)] starting 15m-bot mode=${LIVE_FLAG[*]:-PAPER}"
  python3 -m kalshi_bot "${LIVE_FLAG[@]}" --config "$CONFIG" || true
  echo "[$(date -Is)] 15m-bot exited; restarting in 5s"
  sleep 5
done
