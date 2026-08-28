"""Trend + momentum KXBTC15M engine driven by websocket BRTI."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from kalshi_bot.brti_ws.feed import BRTIFeed
from kalshi_bot.config import AppConfig, BrtiWSConfig
from kalshi_bot.venues.kalshi import KalshiClient, KalshiMarket

logger = logging.getLogger(__name__)


class BrtiTradingEngine:
    def __init__(
        self,
        cfg: AppConfig,
        feed: BRTIFeed,
        kalshi: KalshiClient,
    ):
        self.cfg = cfg
        self.ws_cfg: BrtiWSConfig = cfg.brti_ws
        self.feed = feed
        self.kalshi = kalshi
        self._mid_price_history: deque[tuple[float, float]] = deque()
        self._last_order_ts = 0.0

    @property
    def dry_run(self) -> bool:
        return self.cfg.execution.dry_run

    async def run(self) -> None:
        await self.feed.wait_until_ready()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("BRTI trading engine tick failed; continuing")
            await asyncio.sleep(self.ws_cfg.decision_loop_seconds)

    async def _tick(self) -> None:
        market = await asyncio.to_thread(self._get_active_market)
        if market is None:
            logger.debug("No active %s market found", self.ws_cfg.series_ticker)
            return

        seconds_to_expiry = market.seconds_to_close
        if seconds_to_expiry <= self.ws_cfg.expiry_safeguard_seconds:
            logger.debug(
                "Halting order submission: %.0fs to expiry (<= %ds safeguard) on %s",
                seconds_to_expiry,
                self.ws_cfg.expiry_safeguard_seconds,
                market.ticker,
            )
            return
        if seconds_to_expiry <= 0:
            return

        strike = market.floor_strike
        if strike is None or strike <= 0:
            logger.debug("No strike on active market %s", market.ticker)
            return

        trend_sma = await self.feed.sma(self.ws_cfg.sma_window_seconds)
        latest_tick = await self.feed.latest()
        if trend_sma is None or latest_tick is None:
            logger.debug("Insufficient BRTI history for SMA yet")
            return

        trend_edge = trend_sma - strike

        lagged_price = await self.feed.price_lagged(self.ws_cfg.momentum_lag_seconds)
        if lagged_price is None:
            logger.debug("Insufficient BRTI history for momentum lag yet")
            return
        momentum = latest_tick.price - lagged_price
        momentum_bullish = momentum > 0

        mid_price = self._mid_price(market)
        if mid_price is None:
            logger.debug("No orderbook mid-price available for %s", market.ticker)
            return

        now = time.time()
        self._mid_price_history.append((now, mid_price))
        cutoff = now - self.ws_cfg.mid_price_lag_seconds
        while self._mid_price_history and self._mid_price_history[0][0] < cutoff:
            self._mid_price_history.popleft()
        lagged_mid = self._mid_price_history[0][1] if self._mid_price_history else mid_price
        mid_bullish = mid_price > lagged_mid

        logger.info(
            "%s | strike=%.2f trend_sma=%.2f edge=%.2f momentum=%.2f mid=%.2f "
            "mid_lag=%.2f exp_in=%.0fs",
            market.ticker,
            strike,
            trend_sma,
            trend_edge,
            momentum,
            mid_price,
            lagged_mid,
            seconds_to_expiry,
        )

        if not (
            trend_edge >= self.ws_cfg.min_edge_dollars
            and momentum_bullish
            and mid_bullish
            and mid_price <= self.ws_cfg.max_order_price
        ):
            return

        if now - self._last_order_ts < self.ws_cfg.order_cooldown_seconds:
            return

        logger.info(
            "BUY signal on %s: trend_edge=%.2f momentum=%.2f mid_price=%.2f",
            market.ticker,
            trend_edge,
            momentum,
            mid_price,
        )
        await asyncio.to_thread(self._submit_yes_buy, market.ticker, mid_price)
        self._last_order_ts = now

    def _get_active_market(self) -> KalshiMarket | None:
        markets = self.kalshi.get_markets(self.ws_cfg.series_ticker, status="open", limit=50)
        if not markets:
            return None
        markets.sort(key=lambda item: item.close_time)
        return markets[0]

    @staticmethod
    def _mid_price(market: KalshiMarket) -> float | None:
        if market.yes_bid > 0 and market.yes_ask > 0:
            return (market.yes_bid + market.yes_ask) / 2.0
        if market.yes_ask > 0:
            return market.yes_ask
        if market.yes_bid > 0:
            return market.yes_bid
        return None

    def _submit_yes_buy(self, ticker: str, price_dollars: float) -> None:
        cents = max(1, min(99, int(round(price_dollars * 100))))
        count = int(self.ws_cfg.order_count_contracts)
        if self.dry_run:
            logger.info(
                "[DRY_RUN] would submit YES buy: ticker=%s price=%d¢ count=%d",
                ticker,
                cents,
                count,
            )
            return
        if not self.kalshi.authenticated:
            logger.error("Cannot place live order without Kalshi credentials")
            return
        if not self.cfg.execution.orders_enabled:
            logger.info("orders_enabled=false; skipping live order for %s", ticker)
            return
        self.kalshi.create_order(
            ticker=ticker,
            side="yes",
            action="buy",
            count=count,
            yes_price=cents,
        )
