#!/usr/bin/env python3
"""Kalshi 1-hour WebSocket + crowd-favorite bot launcher.

Run:
    python scripts/kalshi_bot.py
    python scripts/kalshi_bot.py --live
    python -m kalshi_bot --1h-ws
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    args = [sys.executable, "-m", "kalshi_bot", "--1h-ws", *sys.argv[1:]]
    return subprocess.call(args, cwd=root, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
