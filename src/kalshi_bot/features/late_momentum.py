"""Late-window fade, drift, and hammer momentum patterns for 15m settlement."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from kalshi_bot.domain import FeatureSnapshot, TrajectoryState


class LateMomentumPattern(str, Enum):
    NONE = "none"
    DRIFT = "drift"
    HAMMER = "hammer"
    FADE = "fade"


@dataclass(frozen=True)
class LateMomentumAssessment:
    pattern: LateMomentumPattern
    active: bool
    drift_score: float
    hammer_score: float
    fade_score: float
    finish_bias: float
    summary: str


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _finish_direction_signals(features: FeatureSnapshot) -> tuple[float, float, float, bool]:
    """Return trend, velocity, acceleration signals favoring finish above strike."""
    spot = features.current_price
    strike = (
        features.settlement_effective_strike
        if features.settlement_effective_strike is not None
        else features.strike
    )
    above_strike = spot + 1e-9 >= strike
    expected_fraction = max(features.expected_remaining_move / max(spot, 1.0), 1e-6)

    trend_signal = math.tanh(features.short_trend / expected_fraction)
    velocity = features.velocities.get(5, features.velocities.get(10, 0.0))
    velocity_signal = math.tanh(velocity * 500.0)
    acceleration_signal = math.tanh(features.acceleration * 10_000_000.0)

    if not above_strike:
        trend_signal = -trend_signal
        velocity_signal = -velocity_signal
        acceleration_signal = -acceleration_signal
    return trend_signal, velocity_signal, acceleration_signal, above_strike


def assess_late_momentum(
    features: FeatureSnapshot,
    *,
    late_window_seconds: float = 360.0,
    activation_threshold: float = 0.18,
) -> LateMomentumAssessment:
    """
    Detect fade, drift, or hammer during the configured late entry window.

    Uses distance (z), volatility, time remaining, and short-horizon momentum.
    """
    inactive = LateMomentumAssessment(
        pattern=LateMomentumPattern.NONE,
        active=False,
        drift_score=0.0,
        hammer_score=0.0,
        fade_score=0.0,
        finish_bias=0.0,
        summary="outside late window",
    )
    if features.seconds_remaining > late_window_seconds:
        return inactive

    trend_signal, velocity_signal, acceleration_signal, above_strike = (
        _finish_direction_signals(features)
    )
    time_pressure = _clip(1.0 - features.seconds_remaining / late_window_seconds)
    vol_scale = _clip(features.realized_vol / 0.85, 0.35, 1.35)
    z_abs = abs(features.z_distance_to_strike)

    short = features.short_trend
    medium = features.medium_trend
    same_direction = short * medium > 0
    alignment = _clip(abs(math.tanh(short * 5_000.0)) * (1.0 if same_direction else 0.35))

    vel5 = abs(features.velocities.get(5, features.velocities.get(10, 0.0)))
    vel30 = abs(features.velocities.get(30, features.velocities.get(60, 0.0)))
    velocity_ratio = vel5 / max(vel30, 1e-9) if vel30 > 0 else 1.0
    fading_velocity = _clip(1.0 - velocity_ratio, 0.0, 1.0) if vel30 > 0 else 0.0

    # Steady path toward the finish side without a late spike.
    drift_score = _clip(
        0.18 * alignment
        + 0.24 * _clip(trend_signal, 0.0, 1.0)
        + 0.20 * _clip(velocity_signal, 0.0, 1.0)
        + 0.16 * (1.0 - _clip(abs(acceleration_signal), 0.0, 1.0))
        + 0.12 * time_pressure
        + 0.10 * _clip(1.4 - z_abs, 0.0, 1.0)
        - 0.08 * max(0.0, vol_scale - 1.0)
    )

    # Aggressive late push into the finish side, often near the strike.
    hammer_score = _clip(
        0.34 * _clip(acceleration_signal, 0.0, 1.0)
        + 0.26 * _clip(velocity_signal, 0.0, 1.0)
        + 0.18 * time_pressure
        + 0.12 * _clip(1.25 - z_abs, 0.0, 1.0)
        + 0.10 * _clip(velocity_ratio - 0.85, 0.0, 1.0)
    )

    if velocity_ratio < 1.05:
        hammer_score *= 0.55
    if 0.85 <= velocity_ratio <= 1.20 and abs(acceleration_signal) < 0.35:
        drift_score = _clip(drift_score + 0.12)
    decelerating = features.trajectory in {
        TrajectoryState.DECELERATING_UP,
        TrajectoryState.DECELERATING_DOWN,
    }
    reversing = features.trajectory in {
        TrajectoryState.REVERSING_UP,
        TrajectoryState.REVERSING_DOWN,
    }
    extended_move = _clip(z_abs - 0.75, 0.0, 1.0)
    fade_score = _clip(
        max(
            fading_velocity * (0.55 + 0.45 * extended_move) * _clip(1.0 - alignment, 0.40, 1.0),
            (0.80 if decelerating else 0.0) * (0.40 + 0.60 * extended_move),
            0.90 if reversing else 0.0,
        )
        * (0.55 + 0.45 * time_pressure)
        * min(vol_scale, 1.15)
    )

    scores = {
        LateMomentumPattern.DRIFT: drift_score,
        LateMomentumPattern.HAMMER: hammer_score,
        LateMomentumPattern.FADE: fade_score,
    }
    pattern, top_score = max(scores.items(), key=lambda item: item[1])
    if top_score + 1e-12 < activation_threshold:
        return LateMomentumAssessment(
            pattern=LateMomentumPattern.NONE,
            active=True,
            drift_score=drift_score,
            hammer_score=hammer_score,
            fade_score=fade_score,
            finish_bias=0.0,
            summary=(
                f"late window · drift {drift_score:.2f} · hammer {hammer_score:.2f} · "
                f"fade {fade_score:.2f}"
            ),
        )

    if pattern is LateMomentumPattern.FADE:
        finish_bias = -0.65 * _clip(trend_signal, -1.0, 1.0)
        side = "UP" if above_strike else "DOWN"
        summary = (
            f"fade · extended {z_abs:.2f}σ · weakening {side} momentum · "
            f"{features.seconds_remaining:.0f}s left"
        )
    elif pattern is LateMomentumPattern.HAMMER:
        finish_bias = 0.85 * _clip(
            0.55 * trend_signal + 0.45 * velocity_signal,
            -1.0,
            1.0,
        )
        side = "UP" if finish_bias >= 0 else "DOWN"
        summary = (
            f"hammer · {side} push · {z_abs:.2f}σ · vol {features.realized_vol:.2f} · "
            f"{features.seconds_remaining:.0f}s left"
        )
    else:
        finish_bias = 0.70 * _clip(
            0.60 * trend_signal + 0.40 * velocity_signal,
            -1.0,
            1.0,
        )
        side = "UP" if finish_bias >= 0 else "DOWN"
        summary = (
            f"drift · steady {side} · {z_abs:.2f}σ · vol {features.realized_vol:.2f} · "
            f"{features.seconds_remaining:.0f}s left"
        )

    return LateMomentumAssessment(
        pattern=pattern,
        active=True,
        drift_score=drift_score,
        hammer_score=hammer_score,
        fade_score=fade_score,
        finish_bias=finish_bias,
        summary=summary,
    )
