"""Extended regime classification for 1-hour trading."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.domain import FeatureSnapshot, Regime, TrajectoryState
from kalshi_bot.hour.trend_engine import TrendSnapshot
from kalshi_bot.hour.volatility_model import VolatilitySnapshot
from kalshi_bot.models.regime import RegimeConfig, classify_regime


@dataclass(frozen=True)
class HourRegimeConfig(RegimeConfig):
    choppy_consistency: float = 0.35


def classify_hour_regime(
    features: FeatureSnapshot,
    trend: TrendSnapshot,
    vol: VolatilitySnapshot,
    config: HourRegimeConfig | None = None,
) -> Regime:
    config = config or HourRegimeConfig()
    base = classify_regime(features, config)

    if trend.trend_consistency < config.choppy_consistency and abs(trend.short_trend) > config.trend_return:
        return Regime.CHOPPY

    if vol.vol_expansion > 0.35 and features.trajectory in {
        TrajectoryState.ACCELERATING_UP,
        TrajectoryState.ACCELERATING_DOWN,
    }:
        if trend.short_trend > 0:
            return Regime.BREAKOUT
        if trend.short_trend < 0:
            return Regime.BREAKDOWN

    if features.mean_reversion_score > 1.5 and abs(trend.momentum) < config.trend_return:
        return Regime.RANGE

    if base == Regime.CHAOTIC_UNSTABLE:
        return Regime.UNCERTAIN

    if features.data_completeness < 0.5:
        return Regime.UNCERTAIN

    return base
