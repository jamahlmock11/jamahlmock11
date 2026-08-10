"""Cross-venue spot BTC price hub (REST polling; websocket-ready interface)."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpotTick:
    price: float
    source: str
    timestamp: datetime


class SpotPriceHub:
    """Poll Binance + Coinbase spot and keep a short price history."""

    def __init__(self, *, poll_interval_sec: float = 1.0, history_seconds: float = 120.0) -> None:
        self.poll_interval_sec = poll_interval_sec
        self.history_seconds = history_seconds
        self._history: deque[SpotTick] = deque()
        self._latest: SpotTick | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._http = httpx.Client(timeout=5.0)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="spot-hub", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._http.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as exc:
                logger.debug("spot hub refresh failed: %s", exc)
            self._stop.wait(self.poll_interval_sec)

    def refresh(self) -> SpotTick | None:
        ticks: list[SpotTick] = []
        now = datetime.now(timezone.utc)
        try:
            data = self._http.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
            ).json()
            ticks.append(SpotTick(float(data["price"]), "Binance", now))
        except Exception:
            pass
        try:
            data = self._http.get(
                "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
            ).json()
            ticks.append(SpotTick(float(data["price"]), "Coinbase", now))
        except Exception:
            pass
        if not ticks:
            return self._latest
        median = sorted(t.price for t in ticks)[len(ticks) // 2]
        tick = SpotTick(median, "median", now)
        with self._lock:
            self._latest = tick
            self._history.append(tick)
            cutoff = now.timestamp() - self.history_seconds
            while self._history and self._history[0].timestamp.timestamp() < cutoff:
                self._history.popleft()
        return tick

    @property
    def latest(self) -> SpotTick | None:
        with self._lock:
            return self._latest

    def price_at(self, seconds_ago: float) -> float | None:
        if seconds_ago < 0:
            return None
        target = time.time() - seconds_ago
        with self._lock:
            best: SpotTick | None = None
            best_delta = float("inf")
            for tick in self._history:
                delta = abs(tick.timestamp.timestamp() - target)
                if delta < best_delta:
                    best_delta = delta
                    best = tick
        return best.price if best else None

    def move_since(self, seconds: float) -> float | None:
        latest = self.latest
        prior = self.price_at(seconds)
        if latest is None or prior is None:
            return None
        return latest.price - prior
