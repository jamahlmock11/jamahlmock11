#!/usr/bin/env bash
# Run 15-minute (KXBTC15M) and 1-hour (KXBTCD) bots side by side until stopped.
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

LIVE_FLAG=()
if [[ "${1:-}" == "--live" ]]; then
  LIVE_FLAG=(--live)
fi
# Auto-live when credentials are present (e.g. from environment secrets).
if [[ -z "${LIVE_FLAG[*]:-}" && -n "${KALSHI_API_KEY_ID:-}" && -f secrets/kalshi_private.key ]]; then
  LIVE_FLAG=(--live)
fi

CONFIG_15M="${CONFIG_15M:-config/default.yaml}"
CONFIG_1H="${CONFIG_1H:-config/1h.yaml}"

mkdir -p logs

run_with_restart() {
  local name="$1"
  local logfile="$2"
  shift 2
  while true; do
    {
      echo "[$(date -Is)] starting ${name}"
      python3 -m kalshi_bot "$@"
      echo "[$(date -Is)] ${name} exited; restarting in 5s"
    } >>"$logfile" 2>&1
    sleep 5
  done
}

run_with_restart "15m-bot" logs/15m.log "${LIVE_FLAG[@]}" --config "$CONFIG_15M" &
PID_15M=$!

run_with_restart "1h-bot" logs/1h.log "${LIVE_FLAG[@]}" --1h --config "$CONFIG_1H" &
PID_1H=$!

nohup bash scripts/run_dashboard.sh >> logs/dashboard.log 2>&1 &
PID_DASH=$!

echo "15-minute bot pid=${PID_15M} config=${CONFIG_15M} log=logs/15m.log"
echo "1-hour bot pid=${PID_1H} config=${CONFIG_1H} log=logs/1h.log"
echo "Edge Desk dashboard pid=${PID_DASH} url=http://127.0.0.1:8787"
echo "Tail logs: tail -f logs/15m.log logs/1h.log logs/dashboard.log"
echo "Press Ctrl+C to stop bots (dashboard keeps running in background)."

trap 'kill "$PID_15M" "$PID_1H" 2>/dev/null; wait; exit 0' INT TERM

wait -n "$PID_15M" "$PID_1H"
