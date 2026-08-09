"""Multi-timeframe causal feature engineering for 1-hour contracts."""

from __future__ import annotations

from dataclasses import dataclass

from datetime import datetime, timezone

from kalshi_bot.domain import FeatureSnapshot, MarketSnapshot, SupportingAggregate
from kalshi_bot.features.engine import FeatureEngine, FeatureEngineConfig, HORIZONS as BASE_HORIZONS
from kalshi_bot.hour.trend_engine import HOUR_HORIZONS, classify_trend, TrendSnapshot
from kalshi_bot.hour.volatility_model import analyze_volatility, VolatilitySnapshot

HOUR_FEATURE_HORIZONS = HOUR_HORIZONS


@dataclass(frozen=True)
class HourFeatureBundle:
    features: FeatureSnapshot
    trend: TrendSnapshot
    volatility: VolatilitySnapshot


class HourFeatureEngine(FeatureEngine):
    """BRTI history engine with 1-hour lookback horizons."""

    def __init__(self, config: FeatureEngineConfig | None = None) -> None:
        cfg = config or FeatureEngineConfig(history_seconds=3700.0)
        super().__init__(cfg)

    def compute_bundle(
        self,
        market: MarketSnapshot,
        *,
        now=None,
        supporting: SupportingAggregate | None = None,
    ) -> HourFeatureBundle:
        features = self.compute(market, now=now, supporting=supporting)
        trend = classify_trend(dict(features.changes))
        points = [
            p for p in self._history
            if p.timestamp <= features.timestamp
        ]
        prices = [p.price for p in points]
        span = (
            (points[-1].timestamp - points[0].timestamp).total_seconds()
            if len(points) > 1
            else 0.0
        )
        vol = analyze_volatility(
            current_price=features.current_price,
            strike=features.strike,
            seconds_remaining=features.seconds_remaining,
            realized_vol=features.realized_vol,
            changes=dict(features.changes),
            prices=prices,
            timestamps_span=span,
        )
        return HourFeatureBundle(features=features, trend=trend, volatility=vol)

    def compute(
        self,
        market: MarketSnapshot,
        *,
        now=None,
        supporting: SupportingAggregate | None = None,
    ) -> FeatureSnapshot:
        snapshot = super().compute(market, now=now, supporting=supporting)
        extended_changes = dict(snapshot.changes)
        extended_velocities = dict(snapshot.velocities)

        observed_now = snapshot.timestamp
        points = [
            p for p in self._history if p.timestamp <= observed_now
        ]
        if not points:
            return snapshot
        timestamps = [p.timestamp for p in points]
        current = points[-1]

        for horizon in HOUR_HORIZONS:
            if horizon in extended_changes:
                continue
            if horizon in BASE_HORIZONS:
                continue
            past = self._at_or_before(
                points,
                timestamps,
                datetime.fromtimestamp(
                    current.timestamp.timestamp() - horizon,
                    tz=observed_now.tzinfo,
                ),
            )
            if past is None:
                continue
            elapsed = (current.timestamp - past.timestamp).total_seconds()
            if elapsed <= 0 or elapsed < horizon * 0.4:
                continue
            change = current.price / past.price - 1.0
            extended_changes[horizon] = change
            extended_velocities[horizon] = change / elapsed

        completeness = len(extended_changes) / len(HOUR_HORIZONS)
        return FeatureSnapshot(
            timestamp=snapshot.timestamp,
            current_price=snapshot.current_price,
            strike=snapshot.strike,
            seconds_remaining=snapshot.seconds_remaining,
            changes=extended_changes,
            velocities=extended_velocities,
            acceleration=snapshot.acceleration,
            short_trend=snapshot.short_trend,
            medium_trend=snapshot.medium_trend,
            realized_vol=snapshot.realized_vol,
            expected_remaining_move=snapshot.expected_remaining_move,
            z_distance_to_strike=snapshot.z_distance_to_strike,
            mean_reversion_score=snapshot.mean_reversion_score,
            orderbook_imbalance=snapshot.orderbook_imbalance,
            cross_venue_agreement=snapshot.cross_venue_agreement,
            cross_venue_dispersion=snapshot.cross_venue_dispersion,
            data_completeness=min(snapshot.data_completeness, completeness),
            trajectory=snapshot.trajectory,
            sample_count=snapshot.sample_count,
            oldest_sample_age=snapshot.oldest_sample_age,
            rationale=snapshot.rationale,
            settlement_effective_strike=snapshot.settlement_effective_strike,
            settlement_locked_fraction=snapshot.settlement_locked_fraction,
        )
