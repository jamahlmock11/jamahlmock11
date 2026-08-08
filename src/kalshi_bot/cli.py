"""CLI entrypoint."""

from __future__ import annotations

import argparse
import logging
import sys

from rich.console import Console

from kalshi_bot.config import ensure_dirs, load_settings, load_yaml_config, merge_runtime
from kalshi_bot.bot import TradingBot
from kalshi_bot.journal import TradeJournal


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kalshi-bot",
        description=(
            "Kalshi BTC 15m/1h mispricing bot: IBIT options-implied Black-Scholes "
            "probabilities vs Kalshi book, plus Kalshi↔Polymarket cross-venue arb."
        ),
    )
    p.add_argument("--config", default="config/default.yaml", help="YAML config path")
    p.add_argument("--once", action="store_true", help="Single scan cycle then exit")
    p.add_argument("--live", action="store_true", help="Disable dry-run (requires Kalshi keys)")
    p.add_argument("--scan-only", action="store_true", help="Scan and print; never place orders")
    p.add_argument(
        "--dashboard",
        action="store_true",
        help="Serve Edge Desk trade blotter (http://127.0.0.1:8787)",
    )
    p.add_argument("--host", default="0.0.0.0", help="Dashboard bind host")
    p.add_argument("--port", type=int, default=8787, help="Dashboard port")
    p.add_argument("--db", default="data/journal.db", help="SQLite journal path")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ensure_dirs()
    settings = load_settings()
    config = merge_runtime(load_yaml_config(args.config), settings)
    journal = TradeJournal(args.db)

    if args.dashboard:
        import uvicorn
        from kalshi_bot.dashboard.app import create_app

        console = Console()
        console.print(f"[bold]Edge Desk[/bold] → http://{args.host}:{args.port}")
        uvicorn.run(create_app(args.db), host=args.host, port=args.port, log_level="info")
        return 0

    if args.live:
        config.execution.dry_run = False
        settings.dry_run = False
    if args.scan_only:
        config.execution.dry_run = True

    console = Console()
    bot = TradingBot(config, settings, journal=journal)

    if args.scan_only:
        config.execution.only_tiers = []

    try:
        if args.once or args.scan_only:
            bot.once()
            console.print(
                f"Done. signals={bot.stats.signals_seen} "
                f"trades={bot.stats.trades} arbs={bot.stats.arbs} "
                f"journal={args.db}"
            )
            return 0
        bot.run_forever()
        return 0
    finally:
        bot.close()


if __name__ == "__main__":
    sys.exit(main())
