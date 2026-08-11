from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.domain import FeatureSnapshot, Regime, TrajectoryState
from kalshi_bot.models.ensemble import EnsembleProbabilityModel, _brti_settlement_core_probability

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _features(*, seconds_remaining: float = 300.0, z_distance: float = 0.5) -> FeatureSnapshot:
    return FeatureSnapshot(
        timestamp=NOW,
        current_price=65_020,
        strike=65_000,
        seconds_remaining=seconds_remaining,
        changes={5: 0.0002, 10: 0.0003, 15: 0.0004, 30: 0.0005, 60: 0.0006, 120: 0.0007},
        velocities={5: 0.00004, 10: 0.00003, 15: 0.000026},
        acceleration=0.000001,
        short_trend=0.0003,
        medium_trend=0.0006,
        realized_vol=0.65,
        expected_remaining_move=80,
        z_distance_to_strike=z_distance,
        mean_reversion_score=-0.1,
        orderbook_imbalance=0.1,
        cross_venue_agreement=0.9,
        cross_venue_dispersion=0.0002,
        data_completeness=1.0,
        trajectory=TrajectoryState.ACCELERATING_UP,
        sample_count=301,
        oldest_sample_age=300,
    )


def test_brti_settlement_core_is_present_in_ensemble():
    estimate = EnsembleProbabilityModel().estimate(
        _features(),
        Regime.TREND_UP,
        options_volatility=0.7,
    )
    assert "brti_settlement_core" in estimate.component_probabilities
    assert 0.03 <= estimate.component_probabilities["brti_settlement_core"] <= 0.97


def test_brti_settlement_core_weights_time_more_near_expiry():
    early = _brti_settlement_core_probability(
        _features(seconds_remaining=600.0, z_distance=1.5),
        0.65,
        settlement_window_seconds=420.0,
    )
    late = _brti_settlement_core_probability(
        _features(seconds_remaining=90.0, z_distance=1.5),
        0.65,
        settlement_window_seconds=420.0,
    )
    assert late > early


def test_ensemble_near_expiry_favors_above_strike_when_spot_is_high():
    above = EnsembleProbabilityModel().estimate(
        _features(seconds_remaining=120.0, z_distance=1.8),
        Regime.LOW_VOLATILITY,
        options_volatility=0.55,
    )
    below = EnsembleProbabilityModel().estimate(
        _features(seconds_remaining=120.0, z_distance=-1.8),
        Regime.LOW_VOLATILITY,
        options_volatility=0.55,
    )
    assert above.p_up > below.p_up
    assert above.component_probabilities["brti_settlement_core"] > below.component_probabilities["brti_settlement_core"]
