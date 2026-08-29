#!/usr/bin/env bash
# Build the React 1-hour bot dashboard into FastAPI static assets.
set -euo pipefail

cd "$(dirname "$0")/../dashboard-1h"
npm install
npm run build
echo "Built 1h dashboard -> src/kalshi_bot/dashboard/static/1h"
