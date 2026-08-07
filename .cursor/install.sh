#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for jamahlmock11.
#
# The repository currently has no application code, so this script is a safe
# no-op today. It auto-detects common dependency manifests and installs them as
# the project grows, using each ecosystem's pinned lockfile when one is present.
# Keep it idempotent and non-interactive: it may run repeatedly and against
# cached state.
set -euo pipefail

cd "$(dirname "$0")/.."
repo_root="$(pwd)"
did_work=0

echo "==> Bootstrapping environment in ${repo_root}"

# Node.js: prefer the lockfile's package manager, fall back to npm.
if [[ -f package.json ]]; then
  did_work=1
  if [[ -f pnpm-lock.yaml ]]; then
    echo "==> Detected pnpm-lock.yaml; running pnpm install --frozen-lockfile"
    corepack enable >/dev/null 2>&1 || true
    pnpm install --frozen-lockfile
  elif [[ -f yarn.lock ]]; then
    echo "==> Detected yarn.lock; running yarn install --frozen-lockfile"
    corepack enable >/dev/null 2>&1 || true
    yarn install --frozen-lockfile
  elif [[ -f package-lock.json ]]; then
    echo "==> Detected package-lock.json; running npm ci"
    npm ci
  else
    echo "==> No lockfile found; running npm install"
    npm install
  fi
fi

# Python: prefer an isolated virtualenv when the base image supports it
# (ensurepip present); otherwise fall back to a user-site install, which works
# without extra system packages.
if [[ -f requirements.txt || -f pyproject.toml ]]; then
  did_work=1
  if python3 -c "import ensurepip" >/dev/null 2>&1; then
    if [[ ! -d .venv ]]; then
      echo "==> Creating Python virtualenv at .venv"
      python3 -m venv .venv
    fi
    py=".venv/bin/python"
    "$py" -m pip install --upgrade pip >/dev/null
  else
    echo "==> venv unavailable (no ensurepip); using pip --user install"
    py="python3"
    pip_user="--user"
  fi
  if [[ -f requirements.txt ]]; then
    echo "==> Detected requirements.txt; installing dependencies"
    "$py" -m pip install ${pip_user:-} -r requirements.txt
  fi
  if [[ -f pyproject.toml ]]; then
    echo "==> Detected pyproject.toml; installing project (editable)"
    "$py" -m pip install ${pip_user:-} -e . || echo "==> editable install skipped (not an installable project yet)"
  fi
fi

# Go modules.
if [[ -f go.mod ]]; then
  did_work=1
  echo "==> Detected go.mod; running go mod download"
  go mod download
fi

# Rust crates.
if [[ -f Cargo.toml ]]; then
  did_work=1
  echo "==> Detected Cargo.toml; running cargo fetch"
  cargo fetch
fi

if [[ "${did_work}" -eq 0 ]]; then
  echo "==> No dependency manifests found. Nothing to install yet."
  echo "==> Base toolchain is ready (node, python3, go, cargo, java, gcc/make)."
fi

echo "==> Bootstrap complete."
