"""CLI entrypoint."""

from __future__ import annotations

import argparse
import logging
import sys

from rich.console import Console

from kalshi_bot.config import ensure_dirs, load_settings, load_yaml_config, merge_runtime
from kalshi_bot.bot import TradingBot


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

    if args.live:
        config.execution.dry_run = False
        settings.dry_run = False
    if args.scan_only:
        config.execution.dry_run = True

    console = Console()
    bot = TradingBot(config, settings)

    if args.scan_only:
        # Zero out sizing by emptying only_tiers after we still want to see signals —
        # actually just skip execute by temporarily raising max to 0 via only_tiers empty
        # Better: run once but clear only_tiers so size returns 0
        config.execution.only_tiers = []

    try:
        if args.once or args.scan_only:
            bot.once()
            console.print(
                f"Done. signals={bot.stats.signals_seen} "
                f"trades={bot.stats.trades} arbs={bot.stats.arbs}"
            )
            return 0
        bot.run_forever()
        return 0
    finally:
        bot.close()


if __name__ == "__main__":
    sys.exit(main())