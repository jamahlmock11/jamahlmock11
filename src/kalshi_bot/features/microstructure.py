"""Order-book microstructure features for the 15-minute bot."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from kalshi_bot.domain import ContractSide, FeatureSnapshot, OrderBookSnapshot
from kalshi_bot.market.orderbook import depth, imbalance, notional_depth, spread


@dataclass(frozen=True)
class MicrostructureSnapshot:
    """Live microstructure metrics derived from order book history."""

    bid_ask_imbalance: float
    depth_top10_yes: float
    depth_top10_no: float
    depth_top10_total: float
    whale_detected: bool
    whale_side: str | None
    whale_size: float
    cancellation_rate: float
    new_order_pressure: float
    spread_yes: float | None
    spread_no: float | None
    spread_trend: float  # positive = widening
    trade_velocity: float  # book-update events per second
    liquidity_score: float  # 0–100


@dataclass
class _BookTick:
    timestamp: float
    yes_bid_depth: float
    no_bid_depth: float
    yes_spread: float | None
    no_spread: float | None
    largest_level: float


class MicrostructureTracker:
    """Track order-book updates to compute microstructure features."""

    def __init__(
        self,
        *,
        history_seconds: float = 60.0,
        whale_notional_usd: float = 5000.0,
        levels: int = 10,
    ) -> None:
        self.history_seconds = history_seconds
        self.whale_notional_usd = whale_notional_usd
        self.levels = levels
        self._history: deque[_BookTick] = deque()

    def _top_depth(self, book: OrderBookSnapshot, side: ContractSide) -> float:
        levels = book.levels(side, asks=True)[: self.levels]
        return sum(level.size for level in levels)

    def _largest_level(self, book: OrderBookSnapshot) -> tuple[float, str | None]:
        largest = 0.0
        side: str | None = None
        for label, levels in (
            ("YES", book.yes_bids),
            ("NO", book.no_bids),
            ("YES_ASK", book.yes_asks),
            ("NO_ASK", book.no_asks),
        ):
            for level in levels:
                notional = level.price * level.size
                if notional > largest:
                    largest = notional
                    side = label
        return largest, side

    def update(self, book: OrderBookSnapshot) -> None:
        ts = book.timestamp.timestamp()
        yes_spread = spread(book, ContractSide.YES)
        no_spread = spread(book, ContractSide.NO)
        largest, _ = self._largest_level(book)
        self._history.append(
            _BookTick(
                timestamp=ts,
                yes_bid_depth=sum(level.size for level in book.yes_bids),
                no_bid_depth=sum(level.size for level in book.no_bids),
                yes_spread=yes_spread,
                no_spread=no_spread,
                largest_level=largest,
            )
        )
        cutoff = ts - self.history_seconds
        while self._history and self._history[0].timestamp < cutoff:
            self._history.popleft()

    def compute(
        self,
        book: OrderBookSnapshot,
        features: FeatureSnapshot,
    ) -> MicrostructureSnapshot:
        self.update(book)
        depth_yes = self._top_depth(book, ContractSide.YES)
        depth_no = self._top_depth(book, ContractSide.NO)
        depth_total = depth_yes + depth_no
        yes_spread = spread(book, ContractSide.YES)
        no_spread = spread(book, ContractSide.NO)
        largest_notional, whale_side = self._largest_level(book)
        whale_detected = largest_notional >= self.whale_notional_usd

        cancellation_rate = 0.0
        new_order_pressure = 0.0
        spread_trend = 0.0
        trade_velocity = 0.0
        if len(self._history) >= 2:
            first, last = self._history[0], self._history[-1]
            elapsed = max(last.timestamp - first.timestamp, 0.001)
            trade_velocity = (len(self._history) - 1) / elapsed
            depth_delta = (last.yes_bid_depth + last.no_bid_depth) - (
                first.yes_bid_depth + first.no_bid_depth
            )
            new_order_pressure = depth_delta / max(
                first.yes_bid_depth + first.no_bid_depth, 1.0
            )
            spreads = [
                s
                for s in (last.yes_spread, last.no_spread, first.yes_spread, first.no_spread)
                if s is not None
            ]
            if len(spreads) >= 2:
                spread_trend = spreads[-1] - spreads[0]

            depth_drops = 0
            depth_adds = 0
            prev = self._history[0]
            for tick in list(self._history)[1:]:
                prev_total = prev.yes_bid_depth + prev.no_bid_depth
                curr_total = tick.yes_bid_depth + tick.no_bid_depth
                if curr_total < prev_total * 0.95:
                    depth_drops += 1
                elif curr_total > prev_total * 1.05:
                    depth_adds += 1
                prev = tick
            total_changes = depth_drops + depth_adds
            cancellation_rate = depth_drops / total_changes if total_changes else 0.0

        spread_penalty = 0.0
        for s in (yes_spread, no_spread):
            if s is not None:
                spread_penalty = max(spread_penalty, s)
        notional = notional_depth(book, ContractSide.YES) + notional_depth(
            book, ContractSide.NO
        )
        liquidity_raw = (
            min(depth_total / 50.0, 1.0) * 40.0
            + min(notional / 500.0, 1.0) * 30.0
            + max(0.0, 1.0 - spread_penalty / 0.12) * 30.0
        )
        liquidity_score = max(0.0, min(100.0, liquidity_raw))

        return MicrostructureSnapshot(
            bid_ask_imbalance=imbalance(book),
            depth_top10_yes=depth_yes,
            depth_top10_no=depth_no,
            depth_top10_total=depth_total,
            whale_detected=whale_detected,
            whale_side=whale_side if whale_detected else None,
            whale_size=largest_notional if whale_detected else 0.0,
            cancellation_rate=cancellation_rate,
            new_order_pressure=new_order_pressure,
            spread_yes=yes_spread,
            spread_no=no_spread,
            spread_trend=spread_trend,
            trade_velocity=trade_velocity,
            liquidity_score=liquidity_score,
        )
