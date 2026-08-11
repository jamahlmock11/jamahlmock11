"""Strike/time/path reversal signal for open positions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kalshi_bot.domain import ContractSide, FeatureSnapshot, ProbabilityEstimate
from kalshi_bot.models.strike_gravity import assess_strike_gravity

if TYPE_CHECKING:
    from kalshi_bot.config import RiskConfig


@dataclass(frozen=True)
class PositionReversalConfig:
    enabled: bool = True
    window_seconds: float = 420.0
    min_hold_probability: float = 0.42
    late_hold_probability: float = 0.55
    min_z_support: float = -0.50
    wrong_side_seconds: float = 180.0
    min_forecast_probability: float = 0.40
    momentum_tolerance: float = 0.00005


@dataclass(frozen=True)
class PositionReversalAssessment:
    held_side: ContractSide
    seconds_remaining: float
    time_pressure: float
    required_hold_probability: float
    hold_probability: float
    z_support: float
    spot_supports: bool
    momentum_supports: bool
    forecast_supports: bool
    should_reverse: bool
    summary: str
    reason: str


def reversal_config_from_risk(risk: RiskConfig) -> PositionReversalConfig:
    """Build reversal settings from application risk config."""
    return PositionReversalConfig(
        enabled=risk.position_reversal_enabled,
        window_seconds=risk.position_reversal_window_seconds,
        min_hold_probability=risk.position_reversal_min_hold_probability,
        late_hold_probability=risk.position_reversal_late_hold_probability,
        min_z_support=risk.position_reversal_min_z_support,
        wrong_side_seconds=risk.position_reversal_wrong_side_seconds,
        min_forecast_probability=risk.position_reversal_min_forecast_probability,
    )


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _effective_strike(features: FeatureSnapshot) -> float:
    if features.settlement_effective_strike is not None:
        return features.settlement_effective_strike
    return features.strike


def _time_pressure(seconds_remaining: float, *, window_seconds: float) -> float:
    seconds = max(0.0, seconds_remaining)
    if window_seconds <= 0 or seconds >= window_seconds:
        return 0.0
    return _clip(1.0 - seconds / window_seconds)


def evaluate_position_reversal(
    *,
    position_side: ContractSide,
    features: FeatureSnapshot,
    forecast: ProbabilityEstimate,
    cfg: PositionReversalConfig | None = None,
) -> PositionReversalAssessment:
    """
    Decide whether an open position is still on track toward settlement.

    Uses BRTI spot vs strike, remaining time, path-hold probability, momentum,
    and the ensemble forecast. Requirements tighten as expiry approaches.
    """
    cfg = cfg or PositionReversalConfig()
    gravity = assess_strike_gravity(features)
    strike = _effective_strike(features)
    distance = features.current_price - strike
    seconds = max(features.seconds_remaining, 0.0)
    time_pressure = _time_pressure(seconds, window_seconds=cfg.window_seconds)
    required_hold = cfg.min_hold_probability + time_pressure * (
        cfg.late_hold_probability - cfg.min_hold_probability
    )

    if position_side is ContractSide.YES:
        hold_probability = gravity.finish_probability_up
        z_support = features.z_distance_to_strike
        spot_supports = distance + 1e-9 >= 0
        momentum_supports = features.short_trend + 1e-12 >= -cfg.momentum_tolerance
        if distance + 1e-9 < 0:
            momentum_supports = features.short_trend > cfg.momentum_tolerance
        forecast_supports = forecast.p_up + 1e-12 >= cfg.min_forecast_probability
        hold_label = "UP"
    else:
        hold_probability = 1.0 - gravity.finish_probability_up
        z_support = -features.z_distance_to_strike
        spot_supports = distance - 1e-9 <= 0
        momentum_supports = features.short_trend - 1e-12 <= cfg.momentum_tolerance
        if distance - 1e-9 > 0:
            momentum_supports = features.short_trend < -cfg.momentum_tolerance
        forecast_supports = forecast.p_down + 1e-12 >= cfg.min_forecast_probability
        hold_label = "DOWN"

    reasons: list[str] = []
    if hold_probability + 1e-12 < required_hold:
        reasons.append(
            f"path hold {hold_probability:.0%} below {required_hold:.0%} "
            f"({seconds:.0f}s left)"
        )
    if z_support + 1e-12 < cfg.min_z_support:
        reasons.append(f"strike distance {z_support:+.2f}σ against {hold_label}")
    if not spot_supports and seconds + 1e-9 <= cfg.wrong_side_seconds:
        reasons.append(
            f"spot ${features.current_price:,.0f} on wrong side of "
            f"strike ${strike:,.0f} with {seconds:.0f}s left"
        )
    if not momentum_supports and time_pressure >= 0.25:
        reasons.append("momentum is not moving toward the held target")
    if not forecast_supports and time_pressure >= 0.40:
        held_prob = forecast.p_up if position_side is ContractSide.YES else forecast.p_down
        reasons.append(f"ensemble faded to {held_prob:.0%} on the held side")

    critical_wrong_side = (
        not spot_supports
        and seconds + 1e-9 <= cfg.wrong_side_seconds
        and hold_probability + 1e-12 < required_hold
    )
    weak_path = hold_probability + 1e-12 < required_hold and (
        not momentum_supports
        or z_support + 1e-12 < cfg.min_z_support
        or (not spot_supports and time_pressure >= 0.20)
    )
    faded_forecast = (
        not forecast_supports
        and time_pressure >= 0.35
        and hold_probability + 1e-12 < required_hold + 0.03
    )
    early_wrong_side = (
        not spot_supports
        and seconds + 1e-12 <= cfg.wrong_side_seconds
        and hold_probability + 1e-12 < required_hold + 0.08
    )
    should_reverse = cfg.enabled and (
        critical_wrong_side or weak_path or faded_forecast or early_wrong_side
    )

    direction = "above" if distance >= 0 else "below"
    summary = (
        f"{seconds:.0f}s left · spot ${features.current_price:,.2f} vs "
        f"strike ${strike:,.2f} ({distance:+,.0f} · {direction} · "
        f"{features.z_distance_to_strike:+.2f}σ) · path hold {hold_label} "
        f"{hold_probability:.0%} (need {required_hold:.0%})"
    )
    if should_reverse:
        reason = "position reversal: " + "; ".join(reasons)
    else:
        reason = f"held path intact · {summary}"

    return PositionReversalAssessment(
        held_side=position_side,
        seconds_remaining=seconds,
        time_pressure=time_pressure,
        required_hold_probability=required_hold,
        hold_probability=hold_probability,
        z_support=z_support,
        spot_supports=spot_supports,
        momentum_supports=momentum_supports,
        forecast_supports=forecast_supports,
        should_reverse=should_reverse,
        summary=summary,
        reason=reason,
    )
