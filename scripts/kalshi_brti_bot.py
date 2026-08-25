#!/usr/bin/env python3
"""Launcher for the Kalshi 15m BRTI websocket bot (see src/kalshi_bot/brti_bot.py)."""

from kalshi_bot.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["--brti-ws"]))
