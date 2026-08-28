"""Async KXBTC15M bot using Kalshi websocket BRTI + trend/momentum engine."""

from __future__ import annotations

import asyncio
import logging

from rich.console import Console

from kalshi_bot.brti_ws.engine import BrtiTradingEngine
from kalshi_bot.brti_ws.feed import BRTIFeed
from kalshi_bot.config import AppConfig, Settings
from kalshi_bot.venues.kalshi import KalshiClient

logger = logging.getLogger(__name__)
console = Console()


class BrtiTradingBot:
    def __init__(self, config: AppConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self.kalshi = KalshiClient(
            base_url=settings.kalshi_url,
            api_key_id=settings.kalshi_api_key_id,
            private_key_path=settings.kalshi_private_key_path,
        )
        self.feed = BRTIFeed(config.brti_ws, self.kalshi)
        self.engine = BrtiTradingEngine(config, self.feed, self.kalshi)

    def close(self) -> None:
        self.kalshi.close()

    async def run(self) -> None:
        ws_cfg = self.config.brti_ws
        mode = "DRY-RUN" if self.config.execution.dry_run else "LIVE"
        console.print(
            f"[bold]Kalshi 15m BRTI WebSocket Bot[/bold] mode={mode}\n"
            f"Series: {ws_cfg.series_ticker} | SMA: {ws_cfg.sma_window_seconds}s | "
            f"Min edge: ${ws_cfg.min_edge_dollars:.2f}"
        )
        if not self.kalshi.authenticated:
            raise RuntimeError(
                "Kalshi API credentials required for cfbenchmarks_value websocket feed"
            )
        try:
            await asyncio.gather(self.feed.run(), self.engine.run())
        finally:
            await self.feed.close()
            self.close()
