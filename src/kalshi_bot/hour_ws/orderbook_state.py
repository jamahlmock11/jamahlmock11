"""Maintain live orderbook state from Kalshi WebSocket snapshots and deltas."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kalshi_bot.domain import OrderBookSnapshot
from kalshi_bot.market.orderbook import OrderBookError, parse_orderbook_fp


def _level_key(price: float) -> str:
    return f"{price:.4f}"


@dataclass
class LiveOrderbook:
    """Bid-only yes/no books in Kalshi reciprocal format."""

    yes_bids: dict[str, float] = field(default_factory=dict)
    no_bids: dict[str, float] = field(default_factory=dict)

    def to_crowd_dict(self) -> dict[str, Any]:
        yes_bids = sorted(
            ((float(k), v) for k, v in self.yes_bids.items() if v > 0),
            key=lambda item: item[0],
            reverse=True,
        )
        yes_asks = sorted(
            ((1.0 - float(k), v) for k, v in self.no_bids.items() if v > 0),
            key=lambda item: item[0],
        )
        return {
            "yes": {
                "bids": [[price, size] for price, size in yes_bids[:5]],
                "asks": [[price, size] for price, size in yes_asks[:5]],
            }
        }

    def to_snapshot(self) -> OrderBookSnapshot | None:
        if not self.yes_bids and not self.no_bids:
            return None
        try:
            return parse_orderbook_fp(
                {
                    "yes_dollars": [[k, f"{v:.2f}"] for k, v in self.yes_bids.items() if v > 0],
                    "no_dollars": [[k, f"{v:.2f}"] for k, v in self.no_bids.items() if v > 0],
                }
            )
        except OrderBookError:
            return None

    def load_snapshot(self, payload: dict[str, Any]) -> None:
        book = payload.get("orderbook_fp") or payload.get("orderbook") or payload
        self.yes_bids.clear()
        self.no_bids.clear()
        for side_key, target in (("yes", self.yes_bids), ("no", self.no_bids)):
            levels = book.get(f"{side_key}_dollars") or book.get(side_key) or []
            for raw in levels:
                if not isinstance(raw, (list, tuple)) or len(raw) < 2:
                    continue
                price = float(raw[0])
                if price > 1.0:
                    price /= 100.0
                size = float(raw[1])
                if size > 0:
                    target[_level_key(price)] = size

    def apply_delta(self, payload: dict[str, Any]) -> None:
        side = str(payload.get("side") or "yes").lower()
        target = self.yes_bids if side == "yes" else self.no_bids
        price_raw = payload.get("price_dollars", payload.get("price"))
        if price_raw is None:
            return
        price = float(price_raw)
        if price > 1.0:
            price /= 100.0
        delta = float(payload.get("delta_fp", payload.get("delta", 0)))
        key = _level_key(price)
        new_size = target.get(key, 0.0) + delta
        if new_size <= 0:
            target.pop(key, None)
        else:
            target[key] = new_size
