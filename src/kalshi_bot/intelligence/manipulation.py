"""Market manipulation detection: spoofing, walls, iceberg orders."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from kalshi_bot.domain import OrderBookSnapshot


@dataclass(frozen=True)
class ManipulationAssessment:
    """Result of manipulation scan."""

    detected: bool
    confidence_penalty: float  # 0.10–0.20 when detected
    spoofing_score: float
    fake_wall_score: float
    iceberg_score: float
    cancellation_rate: float
    bid_pressure_30s: float
    ask_pressure_30s: float
    reasons: tuple[str, ...]

    @property
    def bid_ask_pressure(self) -> float:
        return self.bid_pressure_30s - self.ask_pressure_30s


@dataclass
class _BookSnapshot:
    timestamp: float
    yes_bid_depth: float
    yes_ask_depth: float
    no_bid_depth: float
    no_ask_depth: float
    yes_bid_notional: float
    yes_ask_notional: float
    largest_yes_bid: float
    largest_no_bid: float


class ManipulationDetector:
    """Track orderbook history to detect spoofing and fake liquidity."""

    def __init__(
        self,
        *,
        history_seconds: float = 60.0,
        wall_size_multiplier: float = 5.0,
        spoof_cancel_threshold: float = 0.60,
        min_penalty: float = 0.10,
        max_penalty: float = 0.20,
    ) -> None:
        self.history_seconds = history_seconds
        self.wall_size_multiplier = wall_size_multiplier
        self.spoof_cancel_threshold = spoof_cancel_threshold
        self.min_penalty = min_penalty
        self.max_penalty = max_penalty
        self._history: deque[_BookSnapshot] = deque()
        self._large_order_events: deque[tuple[float, float, str]] = deque()

    def _snapshot(self, book: OrderBookSnapshot) -> _BookSnapshot:
        yes_bid_depth = sum(level.size for level in book.yes_bids)
        no_bid_depth = sum(level.size for level in book.no_bids)
        yes_ask_depth = sum(level.size for level in book.yes_asks)
        no_ask_depth = sum(level.size for level in book.no_asks)
        largest_yes_bid = max((level.size for level in book.yes_bids), default=0.0)
        largest_no_bid = max((level.size for level in book.no_bids), default=0.0)
        return _BookSnapshot(
            timestamp=book.timestamp.timestamp(),
            yes_bid_depth=yes_bid_depth,
            yes_ask_depth=yes_ask_depth,
            no_bid_depth=no_bid_depth,
            no_ask_depth=no_ask_depth,
            yes_bid_notional=sum(level.price * level.size for level in book.yes_bids),
            yes_ask_notional=sum(level.price * level.size for level in book.yes_asks),
            largest_yes_bid=largest_yes_bid,
            largest_no_bid=largest_no_bid,
        )

    def observe(self, book: OrderBookSnapshot) -> None:
        """Record an orderbook observation for temporal analysis."""
        now = book.timestamp.timestamp()
        snap = self._snapshot(book)
        cutoff = now - self.history_seconds
        while self._history and self._history[0].timestamp < cutoff:
            self._history.popleft()
        while self._large_order_events and self._large_order_events[0][0] < cutoff:
            self._large_order_events.popleft()

        if self._history:
            prev = self._history[-1]
            avg_bid = (prev.yes_bid_depth + prev.no_bid_depth) / 2
            if snap.largest_yes_bid > avg_bid * self.wall_size_multiplier and avg_bid > 0:
                self._large_order_events.append((now, snap.largest_yes_bid, "yes_bid"))
            if snap.largest_no_bid > avg_bid * self.wall_size_multiplier and avg_bid > 0:
                self._large_order_events.append((now, snap.largest_no_bid, "no_bid"))
            # Track disappearances (cancellation proxy)
            if prev.largest_yes_bid > avg_bid * 3 and snap.largest_yes_bid < prev.largest_yes_bid * 0.3:
                self._large_order_events.append((now, prev.largest_yes_bid, "cancel_yes"))
            if prev.largest_no_bid > avg_bid * 3 and snap.largest_no_bid < prev.largest_no_bid * 0.3:
                self._large_order_events.append((now, prev.largest_no_bid, "cancel_no"))

        self._history.append(snap)

    def assess(self, book: OrderBookSnapshot) -> ManipulationAssessment:
        """Evaluate manipulation risk from recent orderbook history."""
        self.observe(book)
        if len(self._history) < 2:
            return ManipulationAssessment(
                detected=False,
                confidence_penalty=0.0,
                spoofing_score=0.0,
                fake_wall_score=0.0,
                iceberg_score=0.0,
                cancellation_rate=0.0,
                bid_pressure_30s=0.0,
                ask_pressure_30s=0.0,
                reasons=(),
            )

        now = book.timestamp.timestamp()
        window_30s = [s for s in self._history if s.timestamp >= now - 30.0]
        reasons: list[str] = []

        # Bid/ask pressure over last 30 seconds
        if len(window_30s) >= 2:
            first, last = window_30s[0], window_30s[-1]
            bid_delta = (last.yes_bid_depth + last.no_bid_depth) - (first.yes_bid_depth + first.no_bid_depth)
            ask_delta = (last.yes_ask_depth + last.no_ask_depth) - (first.yes_ask_depth + first.no_ask_depth)
            total = abs(bid_delta) + abs(ask_delta) + 1e-9
            bid_pressure_30s = bid_delta / total
            ask_pressure_30s = ask_delta / total
        else:
            bid_pressure_30s = 0.0
            ask_pressure_30s = 0.0

        # Fake wall: large resting size far from mid with no trade-through
        avg_bid = statistics_fmean([s.yes_bid_depth + s.no_bid_depth for s in self._history])
        latest = self._history[-1]
        wall_ratio = max(latest.largest_yes_bid, latest.largest_no_bid) / max(avg_bid, 1.0)
        fake_wall_score = _clip01((wall_ratio - 3.0) / 5.0) if wall_ratio > 3.0 else 0.0
        if fake_wall_score > 0.4:
            reasons.append(f"fake order wall detected (ratio {wall_ratio:.1f}x)")

        # Spoofing: high cancellation rate of large orders
        cancellations = sum(1 for _, _, kind in self._large_order_events if kind.startswith("cancel"))
        placements = sum(1 for _, _, kind in self._large_order_events if not kind.startswith("cancel"))
        cancellation_rate = cancellations / max(placements, 1)
        spoofing_score = _clip01(cancellation_rate / self.spoof_cancel_threshold) if placements > 0 else 0.0
        if spoofing_score > 0.5:
            reasons.append(f"spoofing pattern ({cancellation_rate:.0%} large-order cancellation rate)")

        # Iceberg: repeated similar-size replenishment at same level
        iceberg_score = 0.0
        if len(self._history) >= 5:
            bid_sizes = [s.largest_yes_bid for s in list(self._history)[-5:]]
            if max(bid_sizes) > 0:
                variance = statistics_pstdev(bid_sizes) / max(statistics_fmean(bid_sizes), 1.0)
                iceberg_score = _clip01(1.0 - variance) if statistics_fmean(bid_sizes) > avg_bid * 2 else 0.0
                if iceberg_score > 0.6:
                    reasons.append("iceberg order pattern at bid")

        combined = max(spoofing_score, fake_wall_score, iceberg_score * 0.8)
        detected = combined >= 0.45 or len(reasons) > 0
        penalty = 0.0
        if detected:
            penalty = self.min_penalty + (self.max_penalty - self.min_penalty) * combined

        return ManipulationAssessment(
            detected=detected,
            confidence_penalty=penalty,
            spoofing_score=spoofing_score,
            fake_wall_score=fake_wall_score,
            iceberg_score=iceberg_score,
            cancellation_rate=cancellation_rate,
            bid_pressure_30s=bid_pressure_30s,
            ask_pressure_30s=ask_pressure_30s,
            reasons=tuple(reasons) if reasons else ("no spoofing detected",),
        )


def statistics_fmean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def statistics_pstdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics_fmean(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))
