#!/usr/bin/env bash
# Switch the 15-minute bot to LIVE mode; keep the 1-hour bot in PAPER.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="${HOME}/.local/bin:${PATH}"

export DRY_RUN=true
export DRY_RUN_15M=false
export DRY_RUN_1H=true
export BENCHMARK_MODE=kalshi_passthrough
bash scripts/bootstrap_env.sh

set -a
# shellcheck source=/dev/null
source .env
set +a

if [[ -z "${KALSHI_API_KEY_ID:-}" || ! -f secrets/kalshi_private.key ]]; then
  echo "ERROR: Kalshi credentials missing."
  echo "Set environment secrets KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY, then rerun:"
  echo "  bash scripts/go_live_15m.sh"
  exit 1
fi

if [[ "${DRY_RUN_15M:-true}" != "false" ]]; then
  echo "ERROR: DRY_RUN_15M is not false after bootstrap."
  exit 1
fi

if [[ "${DRY_RUN_1H:-false}" != "true" ]]; then
  echo "ERROR: DRY_RUN_1H is not true after bootstrap."
  exit 1
fi

echo "15m live config:"
echo "  KALSHI_API_KEY_ID=${KALSHI_API_KEY_ID:0:6}..."
echo "  BENCHMARK_MODE=${BENCHMARK_MODE:-unset}"
echo "  DRY_RUN=${DRY_RUN} (global)"
echo "  DRY_RUN_15M=${DRY_RUN_15M}"
echo "  DRY_RUN_1H=${DRY_RUN_1H}"

restart_bot() {
  local session="$1"
  local script="$2"
  if tmux -f /exec-daemon/tmux.portal.conf has-session -t "$session" 2>/dev/null; then
    tmux -f /exec-daemon/tmux.portal.conf send-keys -t "$session" C-c
    sleep 1
    tmux -f /exec-daemon/tmux.portal.conf send-keys -t "$session" C-c
    sleep 1
    tmux -f /exec-daemon/tmux.portal.conf send-keys -t "$session" \
      "export PATH=\"\${HOME}/.local/bin:\${PATH}\" && exec bash $script" C-m
    echo "Restarted $session"
  else
    echo "Session $session not found; start via environment terminals"
  fi
}

restart_bot kalshi_15m_bot scripts/run_15m.sh
restart_bot edge_desk scripts/run_dashboard.sh

echo "Done. 1h bot remains PAPER (DRY_RUN_1H=true)."
echo "Tail logs:"
echo "  tmux attach -t kalshi_15m_bot"
echo "Dashboard: http://127.0.0.1:8790"
