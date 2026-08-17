"""Ensemble probability model tuned for 1-hour horizons."""

from __future__ import annotations

import math

from kalshi_bot.config import OrderbookSkewConfig
from kalshi_bot.domain import FeatureSnapshot, ProbabilityEstimate, Regime
from kalshi_bot.hour.trend_engine import TrendClassification, TrendSnapshot
from kalshi_bot.hour.volatility_model import VolatilitySnapshot
from kalshi_bot.models.ensemble import EnsembleProbabilityModel, EnsembleConfig


def _trend_adjustment(trend: TrendSnapshot) -> float:
    mapping = {
        TrendClassification.STRONG_UP: 0.12,
        TrendClassification.UP: 0.06,
        TrendClassification.WEAK_UP: 0.03,
        TrendClassification.NEUTRAL: 0.0,
        TrendClassification.WEAK_DOWN: -0.03,
        TrendClassification.DOWN: -0.06,
        TrendClassification.STRONG_DOWN: -0.12,
    }
    return mapping[trend.classification]


def _breakout_adjustment(features: FeatureSnapshot, vol: VolatilitySnapshot) -> float:
    if vol.vol_expansion > 0.25 and features.short_trend > 0:
        return 0.05
    if vol.vol_expansion > 0.25 and features.short_trend < 0:
        return -0.05
    return 0.0


def _mean_reversion_adjustment(features: FeatureSnapshot, trend: TrendSnapshot) -> float:
    if features.mean_reversion_score > 1.2 and abs(trend.momentum) < 0.0003:
        return -math.tanh(features.mean_reversion_score) * 0.08
    return 0.0


def model_stability(forecast: ProbabilityEstimate) -> float:
    components = list(forecast.component_probabilities.values())
    if len(components) < 2:
        return 0.5
    mean = sum(components) / len(components)
    variance = sum((c - mean) ** 2 for c in components) / len(components)
    return max(0.0, 1.0 - math.sqrt(variance) * 2.0)


class HourProbabilityModel:
    def __init__(
        self,
        ensemble: EnsembleProbabilityModel | None = None,
        model_version: str = "hour-v1.0.0",
        orderbook_skew: OrderbookSkewConfig | None = None,
    ) -> None:
        self.ensemble = ensemble or EnsembleProbabilityModel(
            EnsembleConfig(late_seconds=60.0)
        )
        self.model_version = model_version
        self.orderbook_skew = orderbook_skew

    def estimate(
        self,
        features: FeatureSnapshot,
        regime: Regime,
        trend: TrendSnapshot,
        vol: VolatilitySnapshot,
        *,
        options_volatility: float | None = None,
        market_prior: float | None = None,
        window_regime=None,
    ) -> ProbabilityEstimate:
        base = self.ensemble.estimate(
            features,
            regime,
            options_volatility=options_volatility,
            market_prior=market_prior,
            window_regime=window_regime,
            orderbook_skew=self.orderbook_skew,
        )

        adjustment = (
            _trend_adjustment(trend)
            + _breakout_adjustment(features, vol)
            + _mean_reversion_adjustment(features, trend)
            + trend.trend_consistency * _trend_adjustment(trend) * 0.25
        )

        if regime in {Regime.CHOPPY, Regime.UNCERTAIN}:
            adjustment *= 0.5

        p_up = max(0.03, min(0.97, base.p_up + adjustment))
        confidence = base.confidence
        if regime in {Regime.CHOPPY, Regime.UNCERTAIN, Regime.HIGH_VOLATILITY}:
            confidence *= 0.85
        if trend.trend_consistency >= 0.7:
            confidence = min(1.0, confidence * 1.05)

        agreement = min(
            1.0,
            base.signal_agreement * 0.6 + trend.trend_consistency * 0.4,
        )

        notes = tuple(base.notes) + (
            f"hour model {self.model_version}",
            f"trend={trend.classification.value}",
            f"vol_expansion={vol.vol_expansion:.2f}",
        )

        return ProbabilityEstimate(
            p_up=p_up,
            p_down=1.0 - p_up,
            confidence=confidence,
            signal_agreement=agreement,
            component_probabilities=dict(base.component_probabilities),
            regime=regime,
            raw_p_up=base.raw_p_up,
            calibrated=base.calibrated,
            notes=notes,
        )
