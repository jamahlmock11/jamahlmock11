"""Crowd sentiment engine for the WebSocket 1-hour bot."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np

from kalshi_bot.config import HourWSConfig


@dataclass(frozen=True)
class CrowdSnapshot:
    score: float
    price_score: float
    volume_score: float
    orderbook_score: float
    momentum_score: float
    timestamp: datetime


class CrowdEngine:
    """Tracks crowd sentiment from price, volume, orderbook, and momentum."""

    def __init__(self, cfg: HourWSConfig):
        self.cfg = cfg
        self.sentiment_scores: dict[str, CrowdSnapshot] = {}
        self.price_history: dict[str, deque[float]] = {}

    def _history(self, market_id: str) -> deque[float]:
        if market_id not in self.price_history:
            self.price_history[market_id] = deque(maxlen=100)
        return self.price_history[market_id]

    def update(
        self,
        market_id: str,
        price: float,
        volume: int,
        orderbook: dict[str, Any] | None,
    ) -> CrowdSnapshot:
        history = self._history(market_id)
        history.append(price)

        price_cents = price * 100.0
        crowd_min = self.cfg.crowd_min_cents
        crowd_max = self.cfg.crowd_max_cents

        price_score = 0.0
        if crowd_min <= price_cents <= crowd_max and crowd_max > crowd_min:
            price_score = ((price_cents - crowd_min) / (crowd_max - crowd_min)) * 100.0
            price_score = float(np.clip(price_score, 0.0, 100.0))

        volume_score = min(volume / 1000.0 * 20.0, 30.0)

        orderbook_score = 0.0
        if orderbook:
            bids = orderbook.get("yes", {}).get("bids", [])
            asks = orderbook.get("yes", {}).get("asks", [])
            bid_volume = sum(float(b[1]) for b in bids[:5]) if bids else 0.0
            ask_volume = sum(float(a[1]) for a in asks[:5]) if asks else 1.0
            total = bid_volume + ask_volume
            if total > 0:
                imbalance = bid_volume / total
                orderbook_score = (imbalance - 0.5) * 60.0

        momentum_score = 0.0
        if len(history) > 10:
            recent = list(history)[-10:]
            trend = float(np.polyfit(range(len(recent)), recent, 1)[0])
            momentum_score = float(np.clip(trend * 1000.0, -20.0, 20.0))

        total_score = float(
            np.clip(price_score + volume_score + orderbook_score + momentum_score, 0.0, 100.0)
        )
        snapshot = CrowdSnapshot(
            score=total_score,
            price_score=price_score,
            volume_score=volume_score,
            orderbook_score=orderbook_score,
            momentum_score=momentum_score,
            timestamp=datetime.now(timezone.utc),
        )
        self.sentiment_scores[market_id] = snapshot
        return snapshot

    def get_crowd_bias(self, market_id: str) -> str | None:
        snapshot = self.sentiment_scores.get(market_id)
        if snapshot is None:
            return None
        if snapshot.score > 60.0:
            return "YES"
        if snapshot.score < 40.0:
            return "NO"
        return None

    def get_confidence(self, market_id: str) -> float:
        snapshot = self.sentiment_scores.get(market_id)
        return snapshot.score if snapshot else 0.0
