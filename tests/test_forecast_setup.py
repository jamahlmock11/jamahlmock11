"""Tests for forecast setup score."""

from __future__ import annotations

from datetime import datetime, timezone

from kalshi_bot.domain import FeatureSnapshot, Regime, TrajectoryState
from kalshi_bot.features.enriched import EnrichedFeatures
from kalshi_bot.features.microstructure import MicrostructureSnapshot
from kalshi_bot.features.price_action import PriceActionSnapshot
from kalshi_bot.features.temporal import TemporalSnapshot
from kalshi_bot.strategies.forecast_setup import compute_forecast_setup_score

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _features(**overrides) -> FeatureSnapshot:
    base = {
        "timestamp": NOW,
        "current_price": 65100.0,
        "strike": 65000.0,
        "seconds_remaining": 480.0,
        "changes": {15: 0.00005, 30: 0.00012, 60: 0.00035},
        "velocities": {5: 0.00002},
        "acceleration": -2e-6,
        "short_trend": 0.0004,
        "medium_trend": 0.0005,
        "realized_vol": 0.35,
        "expected_remaining_move": 120.0,
        "z_distance_to_strike": 0.85,
        "mean_reversion_score": -0.2,
        "orderbook_imbalance": -0.15,
        "cross_venue_agreement": 0.72,
        "cross_venue_dispersion": 0.001,
        "data_completeness": 0.9,
        "sample_count": 40,
        "oldest_sample_age": 2.0,
        "rationale": "test",
        "trajectory": TrajectoryState.REVERSING_UP,
        "late_momentum_pattern": "fade",
        "late_momentum_fade": 0.72,
        "late_momentum_hammer": 0.40,
        "late_momentum_drift": 0.55,
        "late_momentum_finish_bias": -0.35,
        "late_momentum_summary": "fade",
    }
    base.update(overrides)
    return FeatureSnapshot(**base)


def _enriched() -> EnrichedFeatures:
    pa = PriceActionSnapshot(
        vwap_distance_pct=-0.2,
        momentum_15s=0.00005,
        momentum_30s=0.00012,
        momentum_60s=0.00035,
        volatility_expansion=1.4,
        recent_high=65200.0,
        recent_low=64950.0,
        support_distance_pct=0.25,
        resistance_distance_pct=0.15,
        breakout_detected=True,
        fake_breakout=True,
        breakout_direction="UP",
    )
    micro = MicrostructureSnapshot(
        bid_ask_imbalance=-0.22,
        depth_top10_yes=80.0,
        depth_top10_no=120.0,
        depth_top10_total=200.0,
        whale_detected=True,
        whale_side="NO_BID",
        whale_size=6000.0,
        cancellation_rate=0.55,
        new_order_pressure=-0.12,
        spread_yes=0.02,
        spread_no=0.02,
        spread_trend=0.01,
        trade_velocity=1.8,
        liquidity_score=75.0,
    )
    temporal = TemporalSnapshot(
        minutes_until_expiration=8.0,
        day_of_week=4,
        hour_of_day=12,
        market_session="US",
        historical_win_rate=0.52,
        historical_sample_count=100,
        minute_bucket=8,
    )
    return EnrichedFeatures(microstructure=micro, price_action=pa, temporal=temporal)


def test_setup_score_in_sweet_spot():
    result = compute_forecast_setup_score(
        _features(),
        _enriched(),
        Regime.REVERSAL_UP,
        seconds_remaining=480.0,
    )
    assert result.score >= 50.0
    assert result.in_sweet_spot is True
    assert result.components.momentum_exhaustion > 0.4


def test_setup_score_outside_sweet_spot():
    result = compute_forecast_setup_score(
        _features(seconds_remaining=30.0),
        _enriched(),
        Regime.RANGE,
        seconds_remaining=30.0,
    )
    assert result.in_sweet_spot is False
