#!/usr/bin/env bash
# Continuous 1-hour KXBTCD bot with auto-restart.
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

CONFIG="${CONFIG_1H:-config/1h.yaml}"
mkdir -p logs

live_flag() {
  local mode="${DRY_RUN_1H:-${DRY_RUN:-true}}"
  if [[ "$mode" == "false" && -n "${KALSHI_API_KEY_ID:-}" && -f secrets/kalshi_private.key ]]; then
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
  echo "[$(date -Is)] starting 1h-bot mode=${LIVE_FLAG[*]:-PAPER}"
  python3 -m kalshi_bot "${LIVE_FLAG[@]}" --1h --config "$CONFIG" || true
  echo "[$(date -Is)] 1h-bot exited; restarting in 5s"
  sleep 5
done
