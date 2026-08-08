"""Regime-aware ensemble probability model for BRTI terminal direction."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from statistics import NormalDist

from kalshi_bot.domain import FeatureSnapshot, ProbabilityEstimate, Regime, TrajectoryState

Calibrator = Callable[[float], float]
SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60


@dataclass(frozen=True)
class EnsembleConfig:
    fallback_volatility: float = 0.60
    minimum_volatility: float = 0.10
    maximum_volatility: float = 2.50
    probability_floor: float = 0.03
    probability_ceiling: float = 0.97
    late_seconds: float = 60.0
    late_probability_floor: float = 0.10
    late_probability_ceiling: float = 0.90
    missing_signal_shrink: float = 0.55
    conflict_shrink: float = 0.60


BASE_WEIGHTS: dict[str, float] = {
    "terminal_distribution": 0.30,
    "strike_distance": 0.18,
    "trajectory_momentum": 0.13,
    "acceleration_reversal": 0.08,
    "trend_mean_reversion": 0.08,
    "orderbook": 0.08,
    "cross_exchange": 0.06,
    "market_prior": 0.05,
    "historical_prior": 0.04,
}


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _terminal_probability(spot: float, strike: float, seconds: float, volatility: float) -> float:
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    years = max(seconds, 1.0) / SECONDS_PER_YEAR
    sigma_root_t = volatility * math.sqrt(years)
    if sigma_root_t <= 0:
        return 0.5
    # Zero-drift lognormal terminal model; unlike an option price this is a forecast.
    d2 = (math.log(spot / strike) - 0.5 * volatility * volatility * years) / sigma_root_t
    return NormalDist().cdf(d2)


def _regime_weights(regime: Regime, seconds_remaining: float) -> dict[str, float]:
    weights = dict(BASE_WEIGHTS)
    if regime in {Regime.TREND_UP, Regime.TREND_DOWN, Regime.BREAKOUT, Regime.BREAKDOWN}:
        weights["trajectory_momentum"] *= 1.55
        weights["trend_mean_reversion"] *= 1.30
        weights["historical_prior"] *= 0.60
    elif regime in {Regime.REVERSAL_UP, Regime.REVERSAL_DOWN}:
        weights["acceleration_reversal"] *= 2.0
        weights["trajectory_momentum"] *= 0.75
    elif regime in {Regime.RANGE, Regime.LOW_VOLATILITY}:
        weights["trend_mean_reversion"] *= 1.50
        weights["orderbook"] *= 0.80
    elif regime in {Regime.HIGH_VOLATILITY, Regime.CHAOTIC_UNSTABLE}:
        weights["terminal_distribution"] *= 1.35
        weights["orderbook"] *= 0.55
        weights["trajectory_momentum"] *= 0.65
    if seconds_remaining <= 60:
        weights["terminal_distribution"] *= 1.55
        weights["strike_distance"] *= 1.45
        weights["historical_prior"] *= 0.40
        weights["market_prior"] *= 0.70
    return weights


def _trajectory_probability(trajectory: TrajectoryState) -> float:
    return {
        TrajectoryState.ACCELERATING_UP: 0.68,
        TrajectoryState.DECELERATING_UP: 0.57,
        TrajectoryState.ACCELERATING_DOWN: 0.32,
        TrajectoryState.DECELERATING_DOWN: 0.43,
        TrajectoryState.REVERSING_UP: 0.64,
        TrajectoryState.REVERSING_DOWN: 0.36,
        TrajectoryState.FLAT: 0.50,
    }[trajectory]


class EnsembleProbabilityModel:
    def __init__(
        self,
        config: EnsembleConfig | None = None,
        *,
        calibrator: Calibrator | None = None,
    ) -> None:
        self.config = config or EnsembleConfig()
        self.calibrator = calibrator

    def estimate(
        self,
        features: FeatureSnapshot,
        regime: Regime,
        *,
        options_volatility: float | None = None,
        market_prior: float | None = None,
        historical_prior: float | None = None,
    ) -> ProbabilityEstimate:
        """Blend independent probability views, then shrink uncertainty honestly."""
        cfg = self.config
        vol_inputs = [
            value
            for value in (features.realized_vol, options_volatility)
            if value is not None and math.isfinite(value) and value > 0
        ]
        volatility = (
            math.sqrt(sum(value * value for value in vol_inputs) / len(vol_inputs))
            if vol_inputs
            else cfg.fallback_volatility
        )
        volatility = _clip(volatility, cfg.minimum_volatility, cfg.maximum_volatility)
        effective_strike = (
            features.settlement_effective_strike
            if features.settlement_effective_strike is not None
            else features.strike
        )
        terminal = (
            0.999
            if effective_strike <= 0
            else _terminal_probability(
                features.current_price,
                effective_strike,
                features.seconds_remaining,
                volatility,
            )
        )
        strike_distance = NormalDist().cdf(features.z_distance_to_strike)

        expected_fraction = max(
            features.expected_remaining_move / features.current_price,
            0.0001,
        )
        momentum_strength = math.tanh(features.short_trend / expected_fraction)
        trajectory_momentum = _clip(
            0.65 * _trajectory_probability(features.trajectory)
            + 0.35 * (0.5 + 0.25 * momentum_strength)
        )
        acceleration_signal = math.tanh(features.acceleration * 10_000_000)
        acceleration_reversal = _clip(
            0.70 * _trajectory_probability(features.trajectory)
            + 0.30 * (0.5 + 0.20 * acceleration_signal)
        )

        trend_signal = math.tanh(
            (features.short_trend + features.medium_trend) / (2 * expected_fraction)
        )
        reversion_signal = math.tanh(features.mean_reversion_score)
        if regime in {Regime.RANGE, Regime.LOW_VOLATILITY}:
            combined_signal = 0.30 * trend_signal + 0.70 * reversion_signal
        else:
            combined_signal = 0.75 * trend_signal + 0.25 * reversion_signal
        trend_mean_reversion = _clip(0.5 + 0.24 * combined_signal)
        orderbook_probability = _clip(0.5 + 0.22 * features.orderbook_imbalance)

        direction = 1.0 if features.short_trend > 0 else -1.0 if features.short_trend < 0 else 0.0
        cross_probability = _clip(
            0.5
            + direction
            * 0.20
            * features.cross_venue_agreement
            * max(0.0, 1.0 - features.cross_venue_dispersion / 0.003)
        )
        components: dict[str, float] = {
            "terminal_distribution": terminal,
            "strike_distance": strike_distance,
            "trajectory_momentum": trajectory_momentum,
            "acceleration_reversal": acceleration_reversal,
            "trend_mean_reversion": trend_mean_reversion,
            "orderbook": orderbook_probability,
            "cross_exchange": cross_probability,
            "market_prior": _clip(market_prior) if market_prior is not None else 0.5,
            "historical_prior": _clip(historical_prior) if historical_prior is not None else 0.5,
        }
        weights = _regime_weights(regime, features.seconds_remaining)
        weight_total = sum(weights.values())
        weighted = sum(components[name] * weights[name] for name in weights) / weight_total

        directional = [value for value in components.values() if abs(value - 0.5) >= 0.02]
        if directional:
            up_weight = sum(value > 0.5 for value in directional)
            down_weight = sum(value < 0.5 for value in directional)
            signal_agreement = max(up_weight, down_weight) / len(directional)
        else:
            signal_agreement = 0.5

        missing_fraction = 1.0 - _clip(features.data_completeness)
        if market_prior is None:
            missing_fraction += 0.08
        if historical_prior is None:
            missing_fraction += 0.08
        if features.cross_venue_agreement <= 0:
            missing_fraction += 0.12
        shrink = 1.0 - cfg.missing_signal_shrink * _clip(missing_fraction)
        shrink *= 1.0 - cfg.conflict_shrink * (1.0 - signal_agreement)
        if regime is Regime.CHAOTIC_UNSTABLE:
            shrink *= 0.55
        if features.seconds_remaining <= cfg.late_seconds:
            shrink *= 0.85
        raw = 0.5 + (weighted - 0.5) * _clip(shrink)

        calibrated = False
        calibrated_value = raw
        if self.calibrator is not None:
            candidate = float(self.calibrator(raw))
            if not math.isfinite(candidate):
                raise ValueError("probability calibrator returned a non-finite value")
            calibrated_value = candidate
            calibrated = True
        floor, ceiling = cfg.probability_floor, cfg.probability_ceiling
        if features.seconds_remaining <= cfg.late_seconds:
            floor = max(floor, cfg.late_probability_floor)
            ceiling = min(ceiling, cfg.late_probability_ceiling)
        p_up = _clip(calibrated_value, floor, ceiling)
        confidence = _clip(
            signal_agreement
            * (0.45 + 0.55 * features.data_completeness)
            * (1.0 - min(features.cross_venue_dispersion / 0.01, 0.5))
        )
        notes = (
            f"volatility={volatility:.6f}",
            f"shrink={shrink:.6f}",
            "late-contract cap applied" if features.seconds_remaining <= cfg.late_seconds else "",
        )
        return ProbabilityEstimate(
            p_up=p_up,
            p_down=1.0 - p_up,
            confidence=confidence,
            signal_agreement=signal_agreement,
            component_probabilities=components,
            regime=regime,
            raw_p_up=raw,
            calibrated=calibrated,
            notes=tuple(note for note in notes if note),
        )


def estimate_probability(
    features: FeatureSnapshot,
    regime: Regime,
    **kwargs: float | None,
) -> ProbabilityEstimate:
    return EnsembleProbabilityModel().estimate(features, regime, **kwargs)
