from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kalshi_bot.domain import FeatureSnapshot, TrajectoryState
from kalshi_bot.features.late_momentum import LateMomentumPattern, assess_late_momentum
from kalshi_bot.models.ensemble import _momentum_finish_probability


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _features(**overrides) -> FeatureSnapshot:
    base = dict(
        timestamp=NOW,
        current_price=65_020.0,
        strike=65_000.0,
        seconds_remaining=90.0,
        changes={5: 0.0002, 10: 0.0003, 15: 0.0004, 30: 0.0005, 60: 0.0006},
        velocities={5: 0.00005, 10: 0.00004, 15: 0.00003, 30: 0.000025, 60: 0.00002},
        acceleration=0.000001,
        short_trend=0.00025,
        medium_trend=0.00022,
        realized_vol=0.65,
        expected_remaining_move=80.0,
        z_distance_to_strike=0.55,
        mean_reversion_score=-0.1,
        orderbook_imbalance=0.1,
        cross_venue_agreement=0.9,
        cross_venue_dispersion=0.0002,
        data_completeness=1.0,
        trajectory=TrajectoryState.ACCELERATING_UP,
        sample_count=120,
        oldest_sample_age=120.0,
    )
    base.update(overrides)
    return FeatureSnapshot(**base)


def test_late_momentum_inactive_outside_window():
    assessment = assess_late_momentum(_features(seconds_remaining=240.0))
    assert assessment.active is False
    assert assessment.pattern is LateMomentumPattern.NONE


def test_detects_drift_with_steady_above_strike_move():
    assessment = assess_late_momentum(
        _features(
            short_trend=0.00030,
            medium_trend=0.00028,
            acceleration=0.00000005,
            velocities={5: 0.000025, 10: 0.000024, 15: 0.000023, 30: 0.000025, 60: 0.000024},
            trajectory=TrajectoryState.ACCELERATING_UP,
            z_distance_to_strike=0.45,
        )
    )
    assert assessment.pattern is LateMomentumPattern.DRIFT
    assert assessment.finish_bias > 0


def test_detects_hammer_with_late_acceleration():
    assessment = assess_late_momentum(
        _features(
            short_trend=0.00045,
            medium_trend=0.00020,
            acceleration=0.000004,
            velocities={5: 0.00008, 10: 0.00006, 15: 0.00004, 30: 0.00002},
            trajectory=TrajectoryState.ACCELERATING_UP,
            z_distance_to_strike=0.35,
            seconds_remaining=45.0,
        )
    )
    assert assessment.pattern is LateMomentumPattern.HAMMER
    assert assessment.finish_bias > 0


def test_detects_fade_when_momentum_weakens():
    assessment = assess_late_momentum(
        _features(
            short_trend=0.00005,
            medium_trend=0.00035,
            acceleration=-0.000002,
            velocities={5: 0.00001, 10: 0.00002, 15: 0.00004, 30: 0.00005},
            trajectory=TrajectoryState.DECELERATING_UP,
            z_distance_to_strike=1.4,
        )
    )
    assert assessment.pattern is LateMomentumPattern.FADE


def test_momentum_probability_shifts_with_hammer():
    steady = _momentum_finish_probability(
        _features(
            short_trend=0.00020,
            medium_trend=0.00018,
            acceleration=0.0000005,
            trajectory=TrajectoryState.ACCELERATING_UP,
        )
    )
    hammer = _momentum_finish_probability(
        _features(
            short_trend=0.00045,
            medium_trend=0.00020,
            acceleration=0.000004,
            velocities={5: 0.00008, 10: 0.00006, 15: 0.00004, 30: 0.00002},
            trajectory=TrajectoryState.ACCELERATING_UP,
            z_distance_to_strike=0.35,
            seconds_remaining=45.0,
        )
    )
    assert hammer > steady
