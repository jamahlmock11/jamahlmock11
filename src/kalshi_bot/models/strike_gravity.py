"""Strike gravity: predict finish side using path dynamics toward strike."""

from __future__ import annotations

import math
from dataclasses import dataclass

from kalshi_bot.domain import FeatureSnapshot


@dataclass(frozen=True)
class StrikeGravityAssessment:
    distance_to_strike: float
    velocity_toward_strike: float
    acceleration_toward_strike: float
    expected_path_fraction: float
    finish_probability_up: float
    seconds_remaining: float


def assess_strike_gravity(features: FeatureSnapshot) -> StrikeGravityAssessment:
    """
    Model whether price is likely to finish above strike using:
    - distance to strike
    - velocity toward strike
    - acceleration
    - expected path over remaining minutes
    """
    effective_strike = (
        features.settlement_effective_strike
        if features.settlement_effective_strike is not None
        else features.strike
    )
    distance = features.current_price - effective_strike
    seconds = max(features.seconds_remaining, 1.0)
    minutes_remaining = seconds / 60.0

    # Velocity toward strike (positive = moving toward strike from below, negative from above)
    price_velocity = features.short_trend * features.current_price
    if distance >= 0:
        velocity_toward = -price_velocity  # moving down toward strike from above
    else:
        velocity_toward = price_velocity  # moving up toward strike from below

    acceleration_toward = features.acceleration * features.current_price
    if distance >= 0:
        acceleration_toward = -acceleration_toward

    expected_move = max(features.expected_remaining_move, 1.0)
    path_projection = distance + price_velocity * seconds + 0.5 * acceleration_toward * seconds * seconds / 2
    expected_path_fraction = path_projection / expected_move

    # Probability of finishing above strike
    z_finish = distance / expected_move
    momentum_adj = math.tanh(price_velocity * seconds / expected_move) * 0.15
    accel_adj = math.tanh(acceleration_toward * 1000) * 0.08
    locked_boost = features.settlement_locked_fraction * 0.25 * (1.0 if distance > 0 else -1.0)

    logit = z_finish * 0.55 + momentum_adj + accel_adj + locked_boost
    finish_up = 1.0 / (1.0 + math.exp(-logit))
    finish_up = max(0.03, min(0.97, finish_up))

    return StrikeGravityAssessment(
        distance_to_strike=distance,
        velocity_toward_strike=velocity_toward,
        acceleration_toward_strike=acceleration_toward,
        expected_path_fraction=expected_path_fraction,
        finish_probability_up=finish_up,
        seconds_remaining=seconds,
    )
