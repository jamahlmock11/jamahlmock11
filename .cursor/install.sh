#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="${HOME}/.local/bin:${PATH}"

echo "==> Installing Kalshi bot dependencies"
if python3 -c "import ensurepip" >/dev/null 2>&1; then
  if [[ ! -d .venv ]]; then
    python3 -m venv .venv || true
  fi
  if [[ -x .venv/bin/python ]]; then
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -e ".[dev]"
    exit 0
  fi
fi

echo "==> venv unavailable; using pip --user"
python3 -m pip install --user -e ".[dev]"
echo "==> Install complete"
