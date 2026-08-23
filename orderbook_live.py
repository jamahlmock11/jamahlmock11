"""Live Kalshi order book state from WebSocket snapshots and deltas."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class BestBidAsk:
    """Top-of-book prices extracted from a Kalshi orderbook message."""

    best_bid_cents: int | None
    best_ask_cents: int | None
    ticker: str = ""

    @property
    def best_bid(self) -> float | None:
        return None if self.best_bid_cents is None else self.best_bid_cents / 100.0

    @property
    def best_ask(self) -> float | None:
        return None if self.best_ask_cents is None else self.best_ask_cents / 100.0


def _price_cents(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value <= 1.0:
        return int(round(value * 100))
    return int(round(value))


def _levels_from_cents_pairs(pairs: Any) -> dict[float, float]:
    """Parse [[price_cents, volume], ...] ladders into dollar-price levels."""
    levels: dict[float, float] = {}
    if not isinstance(pairs, list):
        return levels
    for pair in pairs:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        cents = _price_cents(pair[0])
        size = _f(pair[1])
        if cents is None or size <= 0:
            continue
        price = cents / 100.0
        levels[price] = levels.get(price, 0.0) + size
    return levels


def extract_best_bid_ask(msg_data: dict[str, Any]) -> BestBidAsk | None:
    """
    Extract highest bid and lowest ask from Kalshi orderbook payloads.

    Supports both:
    - Nested format: ``msg.type`` in ``snapshot`` / ``delta`` with ``bids`` / ``asks``
    - Official format: top-level ``orderbook_snapshot`` / ``orderbook_delta``
    """
    msg = msg_data.get("msg") or {}
    ticker = str(msg.get("market_ticker") or msg_data.get("market_ticker") or "")

    inner_type = msg.get("type")
    if inner_type in {"snapshot", "delta"}:
        bids = msg.get("bids") or []
        asks = msg.get("asks") or []
        best_bid_cents = _price_cents(bids[0][0]) if bids else None
        best_ask_cents = _price_cents(asks[0][0]) if asks else None
        return BestBidAsk(best_bid_cents=best_bid_cents, best_ask_cents=best_ask_cents, ticker=ticker)

    outer_type = msg_data.get("type")
    if outer_type == "ticker":
        bid = _price_cents(msg.get("yes_bid_dollars") or msg.get("yes_bid"))
        ask = _price_cents(msg.get("yes_ask_dollars") or msg.get("yes_ask"))
        return BestBidAsk(best_bid_cents=bid, best_ask_cents=ask, ticker=ticker)

    if outer_type in {"orderbook_snapshot", "orderbook_delta"}:
        book = msg.get("orderbook_fp") or msg.get("orderbook") or msg
        yes_bids = _parse_levels(book.get("yes_dollars") or book.get("yes"))
        no_bids = _parse_levels(book.get("no_dollars") or book.get("no"))
        best_bid_cents = _price_cents(max(yes_bids) if yes_bids else None)
        yes_ask = max(0.0, 1.0 - max(no_bids)) if no_bids else None
        best_ask_cents = _price_cents(yes_ask)
        return BestBidAsk(best_bid_cents=best_bid_cents, best_ask_cents=best_ask_cents, ticker=ticker)

    return None


def _parse_levels(raw_levels: Any) -> dict[float, float]:
    levels: dict[float, float] = {}
    if not isinstance(raw_levels, list):
        return levels
    for raw in raw_levels:
        if isinstance(raw, dict):
            price = _f(raw.get("price_dollars") or raw.get("price"))
            size = _f(raw.get("size_fp") or raw.get("size") or raw.get("quantity"))
        elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
            price = _f(raw[0])
            size = _f(raw[1])
        else:
            continue
        if price > 0 and size > 0:
            levels[price] = levels.get(price, 0.0) + size
    return levels


@dataclass
class TopOfBook:
    ticker: str = ""
    yes_bid: float = 0.0
    yes_ask: float = 0.0
    no_bid: float = 0.0
    no_ask: float = 0.0
    yes_bid_size: float = 0.0
    yes_ask_size: float = 0.0
    no_bid_size: float = 0.0
    no_ask_size: float = 0.0
    updated: bool = False

    @property
    def spread_cents(self) -> int:
        if self.yes_bid <= 0 or self.yes_ask <= 0:
            return 99
        return max(0, int(round((self.yes_ask - self.yes_bid) * 100)))


@dataclass
class LiveOrderBook:
    """Thread-safe local cache of the Kalshi YES/NO ladders."""

    ticker: str
    yes_bids: dict[float, float] = field(default_factory=dict)
    no_bids: dict[float, float] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def apply_message(self, payload: dict[str, Any]) -> TopOfBook:
        msg_type = payload.get("type")
        msg = payload.get("msg") or {}
        inner_type = msg.get("type")

        async with self._lock:
            if inner_type in {"snapshot", "delta"}:
                self._apply_bids_asks(msg, replace=inner_type == "snapshot")
            elif msg_type == "orderbook_snapshot":
                self._apply_snapshot(msg)
            elif msg_type == "orderbook_delta":
                self._apply_delta(msg)
            elif msg_type == "ticker":
                self._apply_ticker(msg)
            return self._top_of_book(msg.get("market_ticker") or self.ticker)

    def _apply_bids_asks(self, msg: dict[str, Any], *, replace: bool) -> None:
        """Apply nested bids/asks arrays formatted as [price_cents, volume]."""
        bids = _levels_from_cents_pairs(msg.get("bids"))
        asks = _levels_from_cents_pairs(msg.get("asks"))
        if replace or bids:
            self.yes_bids = bids if replace else {**self.yes_bids, **bids}
        if replace or asks:
            # YES asks are represented as complementary NO bids on Kalshi's bid-only book.
            self.no_bids = (
                {max(0.0, 1.0 - price): size for price, size in asks.items()}
                if replace
                else {**self.no_bids, **{max(0.0, 1.0 - price): size for price, size in asks.items()}}
            )

    def _apply_snapshot(self, msg: dict[str, Any]) -> None:
        book = msg.get("orderbook_fp") or msg.get("orderbook") or msg
        self.yes_bids = _parse_levels(book.get("yes_dollars") or book.get("yes"))
        self.no_bids = _parse_levels(book.get("no_dollars") or book.get("no"))

    def _apply_delta(self, msg: dict[str, Any]) -> None:
        side = str(msg.get("side") or "yes").lower()
        price = _f(msg.get("price_dollars") or msg.get("price"))
        delta = _f(msg.get("delta") or msg.get("size_delta") or msg.get("size"))
        if price <= 0:
            return
        ladder = self.yes_bids if side == "yes" else self.no_bids
        new_size = ladder.get(price, 0.0) + delta
        if new_size <= 0:
            ladder.pop(price, None)
        else:
            ladder[price] = new_size

    def _apply_ticker(self, msg: dict[str, Any]) -> None:
        yes_bid = _f(msg.get("yes_bid_dollars") or msg.get("yes_bid"))
        yes_ask = _f(msg.get("yes_ask_dollars") or msg.get("yes_ask"))
        if yes_bid > 0:
            self.yes_bids[yes_bid] = max(self.yes_bids.get(yes_bid, 0.0), 1.0)
        if yes_ask > 0:
            no_bid = max(0.0, 1.0 - yes_ask)
            if no_bid > 0:
                self.no_bids[no_bid] = max(self.no_bids.get(no_bid, 0.0), 1.0)

    def _top_of_book(self, ticker: str) -> TopOfBook:
        yes_bid = max(self.yes_bids) if self.yes_bids else 0.0
        no_bid = max(self.no_bids) if self.no_bids else 0.0
        yes_ask = max(0.0, 1.0 - no_bid) if no_bid else 0.0
        no_ask = max(0.0, 1.0 - yes_bid) if yes_bid else 0.0
        return TopOfBook(
            ticker=ticker or self.ticker,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            yes_bid_size=self.yes_bids.get(yes_bid, 0.0),
            yes_ask_size=self.no_bids.get(no_bid, 0.0),
            no_bid_size=self.no_bids.get(no_bid, 0.0),
            no_ask_size=self.yes_bids.get(yes_bid, 0.0),
            updated=bool(self.yes_bids or self.no_bids),
        )

    async def snapshot(self) -> TopOfBook:
        async with self._lock:
            return self._top_of_book(self.ticker)


async def handle_orderbook_update(
    msg_data: dict[str, Any],
    book: LiveOrderBook | None = None,
) -> TopOfBook | BestBidAsk | None:
    """
    Callback wrapper: extract best bid/ask and optionally update a LiveOrderBook.

    Usage in main::

        async def on_book(msg):
            state.top_of_book = await handle_orderbook_update(msg, state.live_book)
    """
    best = extract_best_bid_ask(msg_data)
    if best and best.best_bid_cents is not None and best.best_ask_cents is not None:
        logger.debug(
            "Kalshi orderbook update -> best bid: %s¢ | best ask: %s¢",
            best.best_bid_cents,
            best.best_ask_cents,
        )

    if book is not None:
        return await book.apply_message(msg_data)
    return best
