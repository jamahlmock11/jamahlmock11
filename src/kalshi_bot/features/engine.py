"""Causal feature engineering from timestamped primary BRTI observations."""

from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from kalshi_bot.domain import (
    BenchmarkQuote,
    FeatureSnapshot,
    MarketSnapshot,
    RollingPricePoint,
    SupportingAggregate,
    TrajectoryState,
    utc_datetime,
)
from kalshi_bot.market.orderbook import imbalance

HORIZONS = (5, 10, 15, 30, 60, 120, 180, 300)
SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60

FEATURE_RATIONALE: dict[str, str] = {
    "current_price": "The latest primary BRTI observation at or before evaluation time anchors every relative calculation.",
    "strike": "The explicit contract barrier defines the terminal event; it is never inferred from current spot.",
    "seconds_remaining": "Expiration minus evaluation time sets the forecast horizon and volatility scaling.",
    "changes": "Fractional BRTI returns at fixed horizons retain direction and make moves comparable across price levels.",
    "velocities": "Return per second separates the speed of a move from the selected lookback length.",
    "acceleration": "The change from 15-second to 5-second velocity identifies strengthening or fading impulse.",
    "short_trend": "The mean 5/10/15-second return summarizes immediate pressure while reducing one-tick noise.",
    "medium_trend": "The mean 30/60/120-second return captures the contract-scale trend independently of the latest burst.",
    "realized_vol": "Annualized quadratic log-return variation scales recent BRTI turbulence into the terminal distribution.",
    "expected_remaining_move": "Spot times volatility times square-root remaining time translates volatility into expected dollars.",
    "z_distance_to_strike": "Price-minus-strike divided by expected move measures how reachable the settlement barrier is.",
    "mean_reversion_score": "Rolling-mean minus spot in rolling standard-deviation units measures pull back toward recent value.",
    "orderbook_imbalance": "YES versus NO bid depth captures contract demand, bounded to [-1, 1].",
    "cross_venue_agreement": "Venue agreement rewards a tight corroborating median and penalizes disagreement with immediate BRTI direction.",
    "cross_venue_dispersion": "Maximum relative distance from the supporting median exposes venue dislocation and feed risk.",
    "data_completeness": "Available fixed-horizon returns divided by all required horizons quantifies causal history coverage.",
    "trajectory": "Short/medium trend signs plus acceleration distinguish continuation, fading, reversal, and flat paths.",
    "sample_count": "The number of causal primary observations exposes the statistical support behind the snapshot.",
    "oldest_sample_age": "History span verifies that nominal long-horizon features have genuine temporal coverage.",
}


@dataclass(frozen=True)
class FeatureEngineConfig:
    history_seconds: float = 600.0
    flat_return_threshold: float = 0.00005
    venue_neutral_band: float = 0.0002
    venue_dispersion_limit: float = 0.002


def classify_trajectory(
    short_trend: float,
    medium_trend: float,
    acceleration: float,
    *,
    flat_threshold: float = 0.00005,
) -> TrajectoryState:
    if abs(short_trend) <= flat_threshold and abs(medium_trend) <= flat_threshold:
        return TrajectoryState.FLAT
    if short_trend > flat_threshold and medium_trend < -flat_threshold:
        return TrajectoryState.REVERSING_UP
    if short_trend < -flat_threshold and medium_trend > flat_threshold:
        return TrajectoryState.REVERSING_DOWN
    if short_trend > 0:
        return (
            TrajectoryState.ACCELERATING_UP
            if acceleration > 0
            else TrajectoryState.DECELERATING_UP
        )
    return (
        TrajectoryState.ACCELERATING_DOWN
        if acceleration < 0
        else TrajectoryState.DECELERATING_DOWN
    )


class FeatureEngine:
    """Maintain BRTI history and build snapshots using only data known at `now`."""

    def __init__(self, config: FeatureEngineConfig | None = None) -> None:
        self.config = config or FeatureEngineConfig()
        self._history: list[RollingPricePoint] = []

    @property
    def history(self) -> tuple[RollingPricePoint, ...]:
        return tuple(self._history)

    def add_quote(self, quote: BenchmarkQuote) -> None:
        source = quote.source.lower()
        if not quote.primary or ("brti" not in source and "bitcoin real time index" not in source):
            raise ValueError("feature history accepts primary BRTI observations only")
        if not math.isfinite(quote.price) or quote.price <= 0:
            raise ValueError("BRTI history price must be positive and finite")
        point = RollingPricePoint(
            timestamp=quote.timestamp,
            price=quote.price,
            source=quote.source,
            primary=True,
        )
        timestamps = [item.timestamp for item in self._history]
        index = bisect.bisect_left(timestamps, point.timestamp)
        if index < len(self._history) and self._history[index].timestamp == point.timestamp:
            self._history[index] = point
        else:
            self._history.insert(index, point)
        newest = self._history[-1].timestamp
        cutoff = newest.timestamp() - self.config.history_seconds
        self._history = [item for item in self._history if item.timestamp.timestamp() >= cutoff]

    ingest = add_quote

    @staticmethod
    def _at_or_before(
        points: list[RollingPricePoint],
        timestamps: list[datetime],
        target: datetime,
    ) -> RollingPricePoint | None:
        index = bisect.bisect_right(timestamps, target) - 1
        return points[index] if index >= 0 else None

    def compute(
        self,
        market: MarketSnapshot,
        *,
        now: datetime | None = None,
        supporting: SupportingAggregate | None = None,
    ) -> FeatureSnapshot:
        observed_now = utc_datetime(now or datetime.now(timezone.utc))
        # Filtering here is the causal guard even if a future quote was ingested.
        points = [
            point
            for point in self._history
            if point.primary and point.timestamp <= observed_now
        ]
        if not points:
            raise ValueError("no primary BRTI sample exists at or before now")
        timestamps = [point.timestamp for point in points]
        current = points[-1]

        changes: dict[int, float] = {}
        velocities: dict[int, float] = {}
        for horizon in HORIZONS:
            past = self._at_or_before(
                points,
                timestamps,
                datetime.fromtimestamp(current.timestamp.timestamp() - horizon, tz=timezone.utc),
            )
            if past is None:
                continue
            elapsed = (current.timestamp - past.timestamp).total_seconds()
            if elapsed <= 0 or elapsed < horizon * 0.5:
                continue
            change = current.price / past.price - 1.0
            changes[horizon] = change
            velocities[horizon] = change / elapsed

        short_values = [changes[horizon] for horizon in (5, 10, 15) if horizon in changes]
        medium_values = [changes[horizon] for horizon in (30, 60, 120) if horizon in changes]
        short_trend = statistics.fmean(short_values) if short_values else 0.0
        medium_trend = statistics.fmean(medium_values) if medium_values else 0.0
        fast_velocity = velocities.get(5, velocities.get(10, 0.0))
        slow_velocity = velocities.get(15, velocities.get(30, 0.0))
        acceleration = (fast_velocity - slow_velocity) / 10.0

        squared_log_returns = 0.0
        elapsed_seconds = 0.0
        for previous, latest in zip(points, points[1:]):
            elapsed = (latest.timestamp - previous.timestamp).total_seconds()
            if elapsed <= 0:
                continue
            squared_log_returns += math.log(latest.price / previous.price) ** 2
            elapsed_seconds += elapsed
        realized_vol = (
            math.sqrt(squared_log_returns / elapsed_seconds * SECONDS_PER_YEAR)
            if elapsed_seconds > 0
            else 0.0
        )
        seconds_remaining = max(0.0, (market.expiration - observed_now).total_seconds())
        expected_move = current.price * realized_vol * math.sqrt(seconds_remaining / SECONDS_PER_YEAR)
        if expected_move > 0:
            z_distance = (current.price - market.strike) / expected_move
        else:
            z_distance = 0.0

        prices = [point.price for point in points]
        rolling_mean = statistics.fmean(prices)
        rolling_std = statistics.pstdev(prices) if len(prices) > 1 else 0.0
        mean_reversion = (rolling_mean - current.price) / rolling_std if rolling_std > 0 else 0.0

        dispersion = supporting.dispersion if supporting is not None and supporting.healthy else 1.0
        if supporting is None or not supporting.healthy:
            venue_agreement = 0.0
        else:
            basis = supporting.price / current.price - 1.0
            tightness = max(0.0, 1.0 - dispersion / self.config.venue_dispersion_limit)
            if abs(basis) <= self.config.venue_neutral_band or abs(short_trend) <= self.config.flat_return_threshold:
                direction_factor = 1.0
            else:
                direction_factor = 1.0 if basis * short_trend > 0 else 0.0
            venue_agreement = tightness * direction_factor

        completeness = len(changes) / len(HORIZONS)
        trajectory = classify_trajectory(
            short_trend,
            medium_trend,
            acceleration,
            flat_threshold=self.config.flat_return_threshold,
        )
        return FeatureSnapshot(
            timestamp=observed_now,
            current_price=current.price,
            strike=market.strike,
            seconds_remaining=seconds_remaining,
            changes=changes,
            velocities=velocities,
            acceleration=acceleration,
            short_trend=short_trend,
            medium_trend=medium_trend,
            realized_vol=realized_vol,
            expected_remaining_move=expected_move,
            z_distance_to_strike=z_distance,
            mean_reversion_score=mean_reversion,
            orderbook_imbalance=imbalance(market.orderbook),
            cross_venue_agreement=venue_agreement,
            cross_venue_dispersion=dispersion,
            data_completeness=completeness,
            trajectory=trajectory,
            sample_count=len(points),
            oldest_sample_age=(observed_now - points[0].timestamp).total_seconds(),
            rationale=FEATURE_RATIONALE,
        )

    build = compute

