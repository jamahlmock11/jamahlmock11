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
            "Settlement-aware Kalshi BTC 15-minute ensemble forecaster and "
            "safety-gated paper/live execution system."
        ),
    )
    p.add_argument("--config", default="config/default.yaml", help="YAML config path")
    p.add_argument(
        "--1h",
        action="store_true",
        dest="one_hour",
        help="Run the 1-hour KXBTCD bot (uses config/1h.yaml by default)",
    )
    p.add_argument(
        "--1h-ws",
        action="store_true",
        dest="one_hour_ws",
        help="Run the 1-hour WebSocket + crowd-favorite bot (config/1h_ws.yaml)",
    )
    p.add_argument(
        "--brti-ws",
        action="store_true",
        dest="brti_ws",
        help="Run the 15-minute BRTI websocket bot (config/brti_15m.yaml)",
    )
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
    config_path = args.config
    if args.brti_ws and config_path == "config/default.yaml":
        config_path = "config/brti_15m.yaml"
    elif args.one_hour_ws and config_path == "config/default.yaml":
        config_path = "config/1h_ws.yaml"
    elif args.one_hour and config_path == "config/default.yaml":
        config_path = "config/1h.yaml"
    config = merge_runtime(load_yaml_config(config_path), settings)
    if args.brti_ws:
        journal_path = "data/journal_brti_15m.db"
    elif args.one_hour_ws:
        journal_path = "data/journal_1h_ws.db"
    elif args.one_hour:
        journal_path = "data/journal_1h.db"
    else:
        journal_path = args.db
    journal = TradeJournal(journal_path)

    if args.dashboard:
        import uvicorn
        from kalshi_bot.dashboard.app import create_app

        console = Console()
        console.print(f"[bold]Edge Desk[/bold] → http://{args.host}:{args.port}")
        app_db = args.db if args.db != "data/journal.db" else None
        uvicorn.run(create_app(app_db), host=args.host, port=args.port, log_level="info")
        return 0

    if args.live:
        config.execution.dry_run = False
        settings.dry_run = False
    if args.scan_only:
        config.execution.dry_run = True
        config.execution.orders_enabled = False

    console = Console()
    if args.brti_ws:
        import asyncio

        from kalshi_bot.brti_bot import BrtiTradingBot

        bot = BrtiTradingBot(config, settings)
        try:
            asyncio.run(bot.run())
            return 0
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped by user[/yellow]")
            return 0
        finally:
            bot.close()

    if args.one_hour_ws:
        import asyncio

        from kalshi_bot.hour_ws_bot import HourWSBot

        bot = HourWSBot(config, settings, journal=journal)
        try:
            asyncio.run(bot.run())
            return 0
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped by user[/yellow]")
            return 0
        finally:
            bot.close()

    if args.one_hour:
        from kalshi_bot.hour_bot import HourTradingBot

        bot = HourTradingBot(config, settings, journal=journal)
    else:
        bot = TradingBot(config, settings, journal=journal)

    try:
        if args.once or args.scan_only:
            bot.once()
            console.print(
                f"Done. decisions={bot.stats.decisions} "
                f"trades={bot.stats.trades} no_trades={bot.stats.no_trades} "
                f"journal={journal_path}"
            )
            return 0
        bot.run_forever()
        return 0
    finally:
        bot.close()


if __name__ == "__main__":
    sys.exit(main())
