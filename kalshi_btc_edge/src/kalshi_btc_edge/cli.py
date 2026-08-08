from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from kalshi_btc_edge.bot import EdgeBot
from kalshi_btc_edge.config import load_config
from kalshi_btc_edge.models import Confidence
from kalshi_btc_edge.strategies.cross_venue import format_arb
from kalshi_btc_edge.strategies.mispricing import format_signal


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Kalshi BTC edge bot — IBIT smile mispricing + Kalshi/Polymarket "
            "cross-venue scanner (paper-first)."
        )
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="scan",
        choices=["scan", "bot"],
        help="scan = one shot; bot = polling loop",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--min-confidence",
        type=str,
        default=None,
        choices=["HIGH", "MEDIUM", "LOW"],
        help="Override execution.min_confidence for paper fills",
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.config:
        cfg.root = Path(args.config).resolve().parent
    if args.min_confidence:
        cfg.execution.min_confidence = Confidence(args.min_confidence)

    _setup_logging(cfg.logging_level)
    bot = EdgeBot(cfg)

    if args.command == "scan":
        summary = bot.run_once()
        print(
            f"\nBTC spot≈{summary['btc_spot']:.2f}  IBIT≈{summary['ibit_spot']:.2f}  "
            f"markets={summary['markets']}"
        )
        print("\n=== Mispricing signals (non-PASS) ===")
        shown = 0
        for s in summary["signals"]:
            if s.confidence == Confidence.PASS:
                continue
            print(format_signal(s))
            shown += 1
            if shown >= 25:
                break
        if shown == 0:
            print("(none above PASS threshold)")
        print("\n=== Cross-venue flags ===")
        if not summary["arbs"]:
            print("(none)")
        for a in summary["arbs"][:15]:
            print(format_arb(a))
        print(f"\nPaper fills this scan: {len(summary['fills'])}")
        return 0

    # bot loop
    bot.run_loop(once=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
