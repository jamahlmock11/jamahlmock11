"""Strike-distance and hold-direction checks for late crowd-follow entries."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.config import LongshotConfig
from kalshi_bot.domain import ContractSide, FeatureSnapshot, GateFailure
from kalshi_bot.models.strike_gravity import assess_strike_gravity


@dataclass(frozen=True)
class CrowdStrikeHoldAssessment:
    seconds_remaining: float
    minutes_remaining: float
    spot: float
    strike: float
    distance_usd: float
    z_distance: float
    hold_side: ContractSide
    hold_probability: float
    gravity_probability_up: float
    supports_crowd: bool
    summary: str


def _failure(gate: str, reason: str, observed: object, required: object) -> GateFailure:
    return GateFailure(gate=gate, reason=reason, observed=observed, required=required)


def _effective_strike(features: FeatureSnapshot) -> float:
    if features.settlement_effective_strike is not None:
        return features.settlement_effective_strike
    return features.strike


def evaluate_crowd_strike_hold(
    features: FeatureSnapshot,
    *,
    crowd_side: ContractSide,
    cfg: LongshotConfig,
) -> CrowdStrikeHoldAssessment:
    """Score whether spot, time, and price path support holding with the crowd side."""
    strike = _effective_strike(features)
    distance = features.current_price - strike
    gravity = assess_strike_gravity(features)
    hold_probability = (
        gravity.finish_probability_up
        if crowd_side is ContractSide.YES
        else 1.0 - gravity.finish_probability_up
    )
    hold_side = crowd_side

    z_against = (
        features.z_distance_to_strike
        if crowd_side is ContractSide.NO
        else -features.z_distance_to_strike
    )
    z_ok = z_against + 1e-12 <= cfg.late_crowd_max_z_against
    spot_supports = (
        distance + 1e-9 <= 0
        if crowd_side is ContractSide.NO
        else distance + 1e-9 >= 0
    )
    prob_ok = hold_probability + 1e-12 >= cfg.late_crowd_min_hold_probability
    supports_crowd = z_ok and (prob_ok or spot_supports)

    hold_label = "UP" if hold_side is ContractSide.YES else "DOWN"
    direction = "above" if distance >= 0 else "below"
    summary = (
        f"{features.seconds_remaining:.0f}s left · "
        f"spot ${features.current_price:,.2f} vs strike ${strike:,.2f} "
        f"({distance:+,.0f} · {distance / max(strike, 1.0):+.2%} {direction} · "
        f"{features.z_distance_to_strike:+.2f}σ) · "
        f"path hold {hold_label} {hold_probability:.0%}"
    )

    return CrowdStrikeHoldAssessment(
        seconds_remaining=features.seconds_remaining,
        minutes_remaining=features.seconds_remaining / 60.0,
        spot=features.current_price,
        strike=strike,
        distance_usd=distance,
        z_distance=features.z_distance_to_strike,
        hold_side=hold_side,
        hold_probability=hold_probability,
        gravity_probability_up=gravity.finish_probability_up,
        supports_crowd=supports_crowd,
        summary=summary,
    )


def crowd_strike_hold_gate(
    assessment: CrowdStrikeHoldAssessment,
    *,
    cfg: LongshotConfig,
) -> GateFailure | None:
    if not cfg.late_crowd_require_strike_hold:
        return None
    if assessment.supports_crowd:
        return None
    z_against = (
        assessment.z_distance
        if assessment.hold_side is ContractSide.NO
        else -assessment.z_distance
    )
    return _failure(
        "crowd_strike_hold",
        "late crowd entry needs strike distance and path hold to support the favorite",
        (
            assessment.summary,
            z_against,
            assessment.hold_probability,
        ),
        (
            cfg.late_crowd_max_z_against,
            cfg.late_crowd_min_hold_probability,
        ),
    )
