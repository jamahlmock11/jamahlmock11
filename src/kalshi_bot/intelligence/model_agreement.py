"""Multi-model agreement checker for trade confidence."""

from __future__ import annotations

import math
from dataclasses import dataclass

from kalshi_bot.domain import FeatureSnapshot, ProbabilityEstimate, Regime
from kalshi_bot.features.enriched import EnrichedFeatures
from kalshi_bot.models.monte_carlo import simulate_finish_probability
from kalshi_bot.models.strike_gravity import assess_strike_gravity


@dataclass(frozen=True)
class ModelVote:
    """Single model's directional vote."""

    name: str
    p_up: float
    direction: str  # UP, DOWN, NEUTRAL


@dataclass(frozen=True)
class ModelAgreementAssessment:
    """Agreement across gradient boosting, logistic, neural, and time-series proxies."""

    votes: tuple[ModelVote, ...]
    agreement: float  # 0–1
    consensus_direction: str
    models_agree: bool
    dissenting_models: tuple[str, ...]


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _direction(p_up: float, neutral_band: float = 0.05) -> str:
    if p_up >= 0.5 + neutral_band:
        return "UP"
    if p_up <= 0.5 - neutral_band:
        return "DOWN"
    return "NEUTRAL"


def _gradient_boosting_proxy(
    forecast: ProbabilityEstimate,
    features: FeatureSnapshot,
) -> float:
    """Weighted ensemble components as gradient-boosting proxy."""
    components = dict(forecast.component_probabilities)
    if not components:
        return forecast.p_up
    weights = {
        "terminal_distribution": 0.30,
        "trajectory_momentum": 0.20,
        "strike_distance": 0.18,
        "orderbook": 0.12,
        "trend_mean_reversion": 0.10,
        "cross_exchange": 0.10,
    }
    total_w = 0.0
    blended = 0.0
    for name, weight in weights.items():
        if name in components:
            blended += components[name] * weight
            total_w += weight
    return blended / total_w if total_w > 0 else forecast.p_up


def _logistic_regression_proxy(features: FeatureSnapshot) -> float:
    """Linear logit on key features."""
    z = (
        2.5 * features.z_distance_to_strike
        + 80.0 * features.short_trend
        + 40.0 * features.medium_trend
        + 15.0 * features.orderbook_imbalance
        + 10.0 * features.acceleration
    )
    return _clip(1.0 / (1.0 + math.exp(-z)))


def _neural_network_proxy(
    forecast: ProbabilityEstimate,
    enriched: EnrichedFeatures,
) -> float:
    """Non-linear blend of ensemble + microstructure + price action."""
    micro = enriched.microstructure
    price = enriched.price_action
    raw = (
        0.40 * forecast.p_up
        + 0.15 * (0.5 + 0.5 * micro.bid_ask_imbalance)
        + 0.15 * (0.5 + 0.25 * math.tanh(price.momentum_30s * 500))
        + 0.10 * (micro.liquidity_score / 100.0)
        + 0.10 * (0.5 + 0.3 * math.tanh(price.vwap_distance_pct))
        + 0.10 * (0.5 - 0.2 * price.volatility_expansion)
    )
    if price.fake_breakout:
        raw = 0.5 + (raw - 0.5) * 0.5
    return _clip(raw)


def _time_series_proxy(features: FeatureSnapshot, regime: Regime) -> float:
    """ARIMA-like momentum extrapolation."""
    horizon_weight = min(features.seconds_remaining / 900.0, 1.0)
    trend_signal = (
        0.50 * features.short_trend
        + 0.30 * features.medium_trend
        + 0.20 * features.acceleration
    )
    vol_damp = 1.0 / (1.0 + features.realized_vol)
    regime_bias = {
        Regime.TREND_UP: 0.05,
        Regime.TREND_DOWN: -0.05,
        Regime.BREAKOUT: 0.08,
        Regime.BREAKDOWN: -0.08,
        Regime.REVERSAL_UP: 0.06,
        Regime.REVERSAL_DOWN: -0.06,
    }.get(regime, 0.0)
    projected = 0.5 + horizon_weight * trend_signal * 200 * vol_damp + regime_bias
    return _clip(projected)


def assess_model_agreement(
    forecast: ProbabilityEstimate,
    features: FeatureSnapshot,
    enriched: EnrichedFeatures,
    regime: Regime,
    *,
    min_agreement: float = 0.60,
) -> ModelAgreementAssessment:
    """Require agreement between multiple model families before trading."""
    mc = simulate_finish_probability(
        features,
        paths=2000,
        orderbook_bias=features.orderbook_imbalance,
        seed=int(features.timestamp.timestamp()) % 1_000_000,
    )
    gravity = assess_strike_gravity(features)

    model_outputs = {
        "gradient_boosting": _gradient_boosting_proxy(forecast, features),
        "logistic_regression": _logistic_regression_proxy(features),
        "neural_network": _neural_network_proxy(forecast, enriched),
        "time_series": _time_series_proxy(features, regime),
        "monte_carlo": mc.p_up,
        "strike_gravity": gravity.finish_probability_up,
    }

    votes = tuple(
        ModelVote(name=name, p_up=p_up, direction=_direction(p_up))
        for name, p_up in model_outputs.items()
    )

    directions = [v.direction for v in votes if v.direction != "NEUTRAL"]
    if not directions:
        consensus = "NEUTRAL"
        agreement = 0.5
    else:
        up_count = sum(1 for d in directions if d == "UP")
        down_count = len(directions) - up_count
        consensus = "UP" if up_count >= down_count else "DOWN"
        agreement = max(up_count, down_count) / len(directions)

    dissenting = tuple(
        v.name for v in votes if v.direction != "NEUTRAL" and v.direction != consensus
    )
    models_agree = agreement >= min_agreement and consensus != "NEUTRAL"

    return ModelAgreementAssessment(
        votes=votes,
        agreement=agreement,
        consensus_direction=consensus,
        models_agree=models_agree,
        dissenting_models=dissenting,
    )
