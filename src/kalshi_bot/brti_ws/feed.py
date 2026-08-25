"""Authoritative 1-second BRTI feed via Kalshi `cfbenchmarks_value` websocket."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from kalshi_bot.config import BrtiWSConfig
from kalshi_bot.venues.kalshi import KalshiClient
from kalshi_bot.venues.kalshi_ws import KalshiWebSocketClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BRTITick:
    ts_ms: int
    price: float


def parse_cfbenchmarks_value_message(msg: dict[str, Any]) -> BRTITick | None:
    """Parse a Kalshi `cfbenchmarks_value` websocket frame for BRTI."""
    if msg.get("type") != "cfbenchmarks_value":
        return None
    body = msg.get("msg")
    if not isinstance(body, dict):
        return None
    if str(body.get("index_id", "")).upper() != "BRTI":
        return None

    upstream: dict[str, Any] | None = None
    raw_data = body.get("data")
    if isinstance(raw_data, str):
        try:
            parsed = json.loads(raw_data)
            if isinstance(parsed, dict):
                upstream = parsed
        except json.JSONDecodeError:
            return None
    elif isinstance(raw_data, dict):
        upstream = raw_data

    if upstream is None:
        value = body.get("value")
        ts_ms = body.get("time") or body.get("received_at")
    else:
        value = upstream.get("value")
        ts_ms = upstream.get("time") or body.get("received_at")

    if value is None:
        return None
    try:
        price = float(value)
        ts_key = int(ts_ms if ts_ms is not None else time.time() * 1000)
    except (TypeError, ValueError):
        return None
    if price <= 0 or not (price < float("inf")):
        return None
    return BRTITick(ts_ms=ts_key, price=price)


class BRTIFeed:
    """Maintains a rolling local array of 1-second BRTI ticks."""

    def __init__(self, cfg: BrtiWSConfig, kalshi: KalshiClient):
        self.cfg = cfg
        self._ws = KalshiWebSocketClient(kalshi)
        self._ticks: deque[BRTITick] = deque()
        self._lock = asyncio.Lock()
        self._connected = asyncio.Event()

    async def run(self) -> None:
        attempt = 0
        while True:
            try:
                await self._connect_and_stream()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connected.clear()
                attempt += 1
                delay = min(30.0, (2**attempt)) + random.uniform(0, 1.0)
                logger.warning("BRTI feed error (%s); reconnecting in %.1fs", exc, delay)
                await asyncio.sleep(delay)

    async def _connect_and_stream(self) -> None:
        await self._ws.connect()
        self._connected.set()
        await self._ws.subscribe_cfbenchmarks_value(["BRTI"])
        await self._ws.listen(self._handle_message)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        tick = parse_cfbenchmarks_value_message(msg)
        if tick is None:
            return
        async with self._lock:
            self._ticks.append(tick)
            cutoff = tick.ts_ms - int(self.cfg.brti_history_seconds * 1000)
            while self._ticks and self._ticks[0].ts_ms < cutoff:
                self._ticks.popleft()

    async def wait_until_ready(self, timeout: float = 30.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    async def latest(self) -> BRTITick | None:
        async with self._lock:
            return self._ticks[-1] if self._ticks else None

    async def sma(self, window_seconds: int) -> float | None:
        async with self._lock:
            if not self._ticks:
                return None
            now_ms = self._ticks[-1].ts_ms
            cutoff = now_ms - window_seconds * 1000
            window = [tick.price for tick in self._ticks if tick.ts_ms >= cutoff]
            if not window:
                return None
            return sum(window) / len(window)

    async def price_lagged(self, lag_seconds: int) -> float | None:
        async with self._lock:
            if not self._ticks:
                return None
            target_ms = self._ticks[-1].ts_ms - lag_seconds * 1000
            best: BRTITick | None = None
            for tick in self._ticks:
                if tick.ts_ms <= target_ms:
                    best = tick
                else:
                    break
            return best.price if best else None

    async def close(self) -> None:
        await self._ws.close()
