"""BRTI settlement math and multi-venue BTC/USD VWAP websocket aggregator."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import numpy as np

logger = logging.getLogger(__name__)

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
KRAKEN_WS = "wss://ws.kraken.com/v2"
BITSTAMP_WS = "wss://ws.bitstamp.net"


@dataclass
class TradePrint:
    price: float
    volume: float
    venue: str
    ts: float


@dataclass
class BRTIEngine:
    """Tracks the 60-second settlement window and required average hurdle."""

    strike_price: float
    settlement_window: list[float | None] = field(default_factory=lambda: [None] * 60)
    current_spot_price: float = 0.0

    def update_spot(self, price: float) -> None:
        """Call instantly whenever a venue websocket fires."""
        if price > 0:
            self.current_spot_price = price

    def record_settlement_tick(self, second_index: int) -> None:
        """Runs once per second between minute 14:00 and 15:00."""
        if 0 <= second_index < 60:
            self.settlement_window[second_index] = self.current_spot_price

    def reset(self, strike_price: float) -> None:
        self.strike_price = strike_price
        self.settlement_window = [None] * 60
        self.current_spot_price = 0.0

    def calculate_probability_metrics(
        self,
        current_second: int,
        *,
        certainty_distance_usd: float = 500.0,
        certainty_max_remaining: int = 15,
    ) -> dict[str, Any]:
        """Compute the remaining mathematical hurdle required to beat the strike."""
        recorded_ticks = [t for t in self.settlement_window if t is not None]
        num_recorded = len(recorded_ticks)
        num_remaining = 60 - num_recorded

        if num_recorded == 0:
            return {
                "status": "OPEN",
                "recorded_count": 0,
                "remaining_count": 60,
                "required_avg_remaining": self.strike_price,
                "price_distance": self.current_spot_price - self.strike_price,
                "mathematical_certainty": False,
                "current_second": current_second,
            }

        current_sum = float(sum(recorded_ticks))
        target_total_sum = self.strike_price * 60
        remaining_sum_needed = target_total_sum - current_sum
        required_avg_remaining = (
            remaining_sum_needed / num_remaining if num_remaining > 0 else self.current_spot_price
        )
        price_distance = self.current_spot_price - required_avg_remaining
        mathematical_certainty = (
            price_distance > certainty_distance_usd and num_remaining < certainty_max_remaining
        )

        return {
            "status": "TRACKING",
            "recorded_count": num_recorded,
            "remaining_count": num_remaining,
            "required_avg_remaining": required_avg_remaining,
            "price_distance": price_distance,
            "mathematical_certainty": mathematical_certainty,
            "current_second": current_second,
        }


class BRTIWebSocketManager:
    """Connects to Coinbase, Kraken, and Bitstamp and publishes 100ms VWAP."""

    def __init__(self, *, vwap_interval_ms: int = 100, vwap_lookback_sec: float = 1.0) -> None:
        self.vwap_interval_ms = vwap_interval_ms
        self.vwap_lookback_sec = vwap_lookback_sec
        self._trades: deque[TradePrint] = deque(maxlen=50_000)
        self._lock = asyncio.Lock()
        self._vwap = 0.0
        self._latest_price = 0.0
        self._running = False
        self._tasks: list[asyncio.Task[Any]] = []

    @property
    def vwap(self) -> float:
        return self._vwap

    @property
    def current_spot_price(self) -> float:
        return self._latest_price

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._run_coinbase(), name="ws-coinbase"),
            asyncio.create_task(self._run_kraken(), name="ws-kraken"),
            asyncio.create_task(self._run_bitstamp(), name="ws-bitstamp"),
            asyncio.create_task(self._vwap_loop(), name="vwap-loop"),
        ]

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def ingest_trade(self, price: float, volume: float, venue: str) -> None:
        if price <= 0 or volume <= 0:
            return
        trade = TradePrint(price=price, volume=volume, venue=venue, ts=time.time())
        async with self._lock:
            self._trades.append(trade)
            self._latest_price = price

    async def _recompute_vwap(self) -> float:
        cutoff = time.time() - self.vwap_lookback_sec
        async with self._lock:
            recent = [t for t in self._trades if t.ts >= cutoff]
            if not recent:
                return self._vwap
            prices = np.array([t.price for t in recent], dtype=float)
            volumes = np.array([t.volume for t in recent], dtype=float)
            total_vol = volumes.sum()
            if total_vol <= 0:
                return self._vwap
            self._vwap = float(np.dot(prices, volumes) / total_vol)
            self._latest_price = float(prices[-1])
            return self._vwap

    async def vwap_stream(self) -> AsyncIterator[float]:
        """Async generator yielding VWAP every ``vwap_interval_ms``."""
        while self._running:
            yield await self._recompute_vwap()
            await asyncio.sleep(self.vwap_interval_ms / 1000.0)

    async def _vwap_loop(self) -> None:
        while self._running:
            await self._recompute_vwap()
            await asyncio.sleep(self.vwap_interval_ms / 1000.0)

    async def _run_coinbase(self) -> None:
        await self._ws_loop(
            COINBASE_WS,
            self._coinbase_subscribe,
            self._parse_coinbase,
            "coinbase",
        )

    async def _run_kraken(self) -> None:
        await self._ws_loop(
            KRAKEN_WS,
            self._kraken_subscribe,
            self._parse_kraken,
            "kraken",
        )

    async def _run_bitstamp(self) -> None:
        await self._ws_loop(
            BITSTAMP_WS,
            self._bitstamp_subscribe,
            self._parse_bitstamp,
            "bitstamp",
        )

    async def _ws_loop(self, url: str, subscribe, parse, venue: str) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("websockets package is required: pip install websockets") from exc

        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    await subscribe(ws)
                    backoff = 1.0
                    async for raw in ws:
                        if not self._running:
                            break
                        price, volume = parse(raw)
                        if price is not None and volume is not None:
                            await self.ingest_trade(price, volume, venue)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("%s websocket error: %s; reconnecting in %.1fs", venue, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    @staticmethod
    async def _coinbase_subscribe(ws) -> None:
        await ws.send(
            json.dumps(
                {
                    "type": "subscribe",
                    "channels": [{"name": "matches", "product_ids": ["BTC-USD"]}],
                }
            )
        )

    @staticmethod
    def _parse_coinbase(raw: str | bytes) -> tuple[float | None, float | None]:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
        if msg.get("type") != "match":
            return None, None
        return float(msg["price"]), float(msg.get("size") or 0.0)

    @staticmethod
    async def _kraken_subscribe(ws) -> None:
        await ws.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "params": {"channel": "trade", "symbol": ["BTC/USD"], "snapshot": False},
                }
            )
        )

    @staticmethod
    def _parse_kraken(raw: str | bytes) -> tuple[float | None, float | None]:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
        if msg.get("channel") != "trade":
            return None, None
        data = msg.get("data") or []
        if not data:
            return None, None
        trade = data[-1]
        return float(trade["price"]), float(trade.get("qty") or trade.get("volume") or 0.0)

    @staticmethod
    async def _bitstamp_subscribe(ws) -> None:
        await ws.send(
            json.dumps(
                {
                    "event": "bts:subscribe",
                    "data": {"channel": "live_trades_btcusd"},
                }
            )
        )

    @staticmethod
    def _parse_bitstamp(raw: str | bytes) -> tuple[float | None, float | None]:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
        if msg.get("event") != "trade" or msg.get("channel") != "live_trades_btcusd":
            return None, None
        data = msg.get("data") or {}
        return float(data.get("price") or 0.0), float(data.get("amount") or 0.0)
