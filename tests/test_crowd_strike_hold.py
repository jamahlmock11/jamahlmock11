"""Tests for crowd strike hold without model theory."""

from __future__ import annotations

from datetime import datetime, timezone

from kalshi_bot.config import LongshotConfig
from kalshi_bot.domain import ContractSide, FeatureSnapshot, TrajectoryState
from kalshi_bot.strategies.crowd_strike_hold import (
    crowd_strike_hold_gate,
    evaluate_crowd_strike_hold,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def features(
    *,
    spot: float,
    strike: float,
    seconds_remaining: float,
    z_distance: float,
):
    return FeatureSnapshot(
        timestamp=NOW,
        current_price=spot,
        strike=strike,
        seconds_remaining=seconds_remaining,
        changes={},
        velocities={},
        acceleration=0.0,
        short_trend=0.0,
        medium_trend=0.0,
        realized_vol=0.5,
        expected_remaining_move=abs(spot - strike) / max(abs(z_distance), 0.1),
        z_distance_to_strike=z_distance,
        mean_reversion_score=0.0,
        orderbook_imbalance=0.0,
        cross_venue_agreement=1.0,
        cross_venue_dispersion=0.0,
        data_completeness=1.0,
        trajectory=TrajectoryState.FLAT,
        sample_count=100,
        oldest_sample_age=0.0,
    )


def test_strike_hold_supports_no_when_spot_below_strike():
    cfg = LongshotConfig()
    assessment = evaluate_crowd_strike_hold(
        features(spot=63_900.0, strike=63_915.0, seconds_remaining=400, z_distance=-0.4),
        crowd_side=ContractSide.NO,
        cfg=cfg,
    )
    assert assessment.hold_side is ContractSide.NO
    assert assessment.supports_crowd
    assert "path hold DOWN" in assessment.summary
    assert crowd_strike_hold_gate(assessment, cfg=cfg) is None


def test_strike_hold_blocks_no_when_spot_far_above_strike():
    cfg = LongshotConfig(late_crowd_max_z_against=1.0)
    assessment = evaluate_crowd_strike_hold(
        features(spot=64_200.0, strike=63_500.0, seconds_remaining=400, z_distance=2.5),
        crowd_side=ContractSide.NO,
        cfg=cfg,
    )
    assert not assessment.supports_crowd
    failure = crowd_strike_hold_gate(assessment, cfg=cfg)
    assert failure is not None
    assert failure.gate == "crowd_strike_hold"
