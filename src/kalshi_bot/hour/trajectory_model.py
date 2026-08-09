"""Trajectory estimation for remaining contract life."""

from __future__ import annotations

import math
from dataclasses import dataclass

from kalshi_bot.domain import Direction, TrajectoryState
from kalshi_bot.hour.trend_engine import TrendSnapshot


HORIZON_MINUTES = (5, 10, 15, 30, 45, 60)


@dataclass(frozen=True)
class TrajectoryForecast:
    current_direction: Direction
    expected_expiration_direction: Direction
    trajectory_state: TrajectoryState
    expected_moves: dict[int, float]
    strike_cross_probability: float
    finish_above_probability: float


def forecast_trajectory(
    *,
    current_price: float,
    strike: float,
    seconds_remaining: float,
    trend: TrendSnapshot,
    realized_vol: float,
    trajectory: TrajectoryState,
    z_distance: float,
) -> TrajectoryForecast:
    years_remaining = max(seconds_remaining, 1.0) / (365.25 * 24 * 3600)
    vol_move = current_price * realized_vol * math.sqrt(years_remaining)

    expected_moves: dict[int, float] = {}
    for minutes in HORIZON_MINUTES:
        frac = min(minutes * 60.0, seconds_remaining) / max(seconds_remaining, 1.0)
        drift = trend.short_trend * current_price * minutes * 60
        expected_moves[minutes] = drift + vol_move * math.sqrt(frac) * 0.5

    if trend.short_trend > 0.00005:
        current_direction = Direction.UP
    elif trend.short_trend < -0.00005:
        current_direction = Direction.DOWN
    else:
        current_direction = Direction.FLAT

    composite = trend.short_trend + 0.5 * trend.medium_trend + 0.25 * trend.long_trend
    if composite > 0.0001 or z_distance > 0.25:
        expected_dir = Direction.UP
    elif composite < -0.0001 or z_distance < -0.25:
        expected_dir = Direction.DOWN
    else:
        expected_dir = Direction.FLAT

    if trajectory in {TrajectoryState.REVERSING_UP, TrajectoryState.ACCELERATING_UP}:
        expected_dir = Direction.UP
    elif trajectory in {TrajectoryState.REVERSING_DOWN, TrajectoryState.ACCELERATING_DOWN}:
        expected_dir = Direction.DOWN

    from statistics import NormalDist

    if vol_move > 0:
        cross_z = (strike - current_price) / vol_move
        strike_cross = 1.0 - NormalDist().cdf(cross_z)
        finish_above = NormalDist().cdf(z_distance)
    else:
        strike_cross = 0.5
        finish_above = 0.5 if current_price >= strike else 0.0

    return TrajectoryForecast(
        current_direction=current_direction,
        expected_expiration_direction=expected_dir,
        trajectory_state=trajectory,
        expected_moves=expected_moves,
        strike_cross_probability=strike_cross,
        finish_above_probability=finish_above,
    )
