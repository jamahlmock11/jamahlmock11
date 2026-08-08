"""Deterministic market-regime classification from forecasting features."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.domain import FeatureSnapshot, Regime, TrajectoryState


@dataclass(frozen=True)
class RegimeConfig:
    high_volatility: float = 1.00
    low_volatility: float = 0.25
    trend_return: float = 0.0005
    breakout_return: float = 0.0015
    breakout_z: float = 1.0
    chaotic_dispersion: float = 0.003
    minimum_cross_venue_agreement: float = 0.35


def classify_regime(
    features: FeatureSnapshot,
    config: RegimeConfig | None = None,
) -> Regime:
    """Apply stable priority rules; unsafe conflicts dominate tradeable regimes."""
    config = config or RegimeConfig()
    short = features.short_trend
    medium = features.medium_trend
    latest = features.changes.get(15, features.changes.get(30, short))
    conflicting_trends = (
        abs(short) >= config.trend_return
        and abs(medium) >= config.trend_return
        and short * medium < 0
    )
    corroboration_conflict = (
        features.cross_venue_agreement < config.minimum_cross_venue_agreement
        and abs(short) >= config.trend_return
    )
    if (
        features.cross_venue_dispersion >= config.chaotic_dispersion
        or (conflicting_trends and corroboration_conflict)
    ):
        return Regime.CHAOTIC_UNSTABLE
    if features.trajectory is TrajectoryState.REVERSING_UP:
        return Regime.REVERSAL_UP
    if features.trajectory is TrajectoryState.REVERSING_DOWN:
        return Regime.REVERSAL_DOWN
    if latest >= config.breakout_return and features.z_distance_to_strike >= config.breakout_z:
        return Regime.BREAKOUT
    if latest <= -config.breakout_return and features.z_distance_to_strike <= -config.breakout_z:
        return Regime.BREAKDOWN
    if features.realized_vol >= config.high_volatility:
        return Regime.HIGH_VOLATILITY
    if short >= config.trend_return and medium >= 0:
        return Regime.TREND_UP
    if short <= -config.trend_return and medium <= 0:
        return Regime.TREND_DOWN
    if features.realized_vol <= config.low_volatility:
        return Regime.LOW_VOLATILITY
    return Regime.RANGE


class RegimeClassifier:
    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.config = config or RegimeConfig()

    def classify(self, features: FeatureSnapshot) -> Regime:
        return classify_regime(features, self.config)
