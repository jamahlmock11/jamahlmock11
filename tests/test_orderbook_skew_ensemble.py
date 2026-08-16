"""Tests for orderbook_skew as a forecast ensemble component."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kalshi_bot.config import OrderbookSkewConfig
from kalshi_bot.domain import FeatureSnapshot, Regime, TrajectoryState
from kalshi_bot.market.orderbook import parse_orderbook_fp, skew_top_n
from kalshi_bot.models.ensemble import EnsembleProbabilityModel

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _features(
    *,
    seconds_remaining: float = 480.0,
    z_distance: float = 2.0,
    yes_top_skew: float = 0.0,
    no_top_skew: float = 0.0,
) -> FeatureSnapshot:
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
        orderbook_imbalance=0.0,
        yes_top_skew=yes_top_skew,
        no_top_skew=no_top_skew,
        cross_venue_agreement=0.9,
        cross_venue_dispersion=0.0002,
        data_completeness=1.0,
        trajectory=TrajectoryState.ACCELERATING_UP,
        sample_count=301,
        oldest_sample_age=300,
    )


def test_skew_top_n_on_no_book():
    book = parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4800", "100"], ["0.4600", "100"]],
                "no_dollars": [["0.4800", "300"], ["0.4600", "50"]],
            }
        },
        timestamp=NOW,
    )
    from kalshi_bot.domain import ContractSide

    no_skew = skew_top_n(book, n=5, side=ContractSide.NO)
    yes_skew = skew_top_n(book, n=5, side=ContractSide.YES)
    assert no_skew > 0.25
    assert no_skew > yes_skew


def test_orderbook_skew_component_absent_when_disabled():
    cfg = OrderbookSkewConfig(ensemble_enabled=False)
    estimate = EnsembleProbabilityModel().estimate(
        _features(yes_top_skew=0.40),
        Regime.TREND_UP,
        orderbook_skew=cfg,
    )
    assert "orderbook_skew" not in estimate.component_probabilities


def test_orderbook_skew_component_active_within_nine_minutes():
    cfg = OrderbookSkewConfig(
        ensemble_enabled=True,
        ensemble_max_seconds_remaining=540.0,
        min_skew=0.25,
        min_z_distance=1.5,
    )
    estimate = EnsembleProbabilityModel().estimate(
        _features(seconds_remaining=480.0, yes_top_skew=0.40),
        Regime.TREND_UP,
        orderbook_skew=cfg,
    )
    assert "orderbook_skew" in estimate.component_probabilities
    assert estimate.component_probabilities["orderbook_skew"] > 0.5
    assert any("orderbook_skew" in note for note in estimate.notes)


def test_orderbook_skew_active_on_no_book_only():
    cfg = OrderbookSkewConfig(
        ensemble_enabled=True,
        ensemble_max_seconds_remaining=540.0,
        min_skew=0.25,
        min_z_distance=1.5,
    )
    estimate = EnsembleProbabilityModel().estimate(
        _features(seconds_remaining=300.0, yes_top_skew=0.05, no_top_skew=0.40),
        Regime.TREND_UP,
        orderbook_skew=cfg,
    )
    assert "orderbook_skew" in estimate.component_probabilities
    assert estimate.component_probabilities["orderbook_skew"] < 0.5


def test_orderbook_skew_component_off_outside_nine_minutes():
    cfg = OrderbookSkewConfig(
        ensemble_enabled=True,
        ensemble_max_seconds_remaining=540.0,
        min_skew=0.25,
        min_z_distance=1.5,
    )
    estimate = EnsembleProbabilityModel().estimate(
        _features(seconds_remaining=600.0, yes_top_skew=0.40),
        Regime.TREND_UP,
        orderbook_skew=cfg,
    )
    assert "orderbook_skew" not in estimate.component_probabilities


def test_orderbook_skew_directional_effect_on_p_up():
    cfg = OrderbookSkewConfig(
        ensemble_enabled=True,
        ensemble_max_seconds_remaining=540.0,
        min_skew=0.25,
        min_z_distance=1.5,
    )
    bid_heavy = EnsembleProbabilityModel().estimate(
        _features(seconds_remaining=300.0, yes_top_skew=0.40, no_top_skew=0.0),
        Regime.TREND_UP,
        orderbook_skew=cfg,
    )
    ask_heavy_no = EnsembleProbabilityModel().estimate(
        _features(seconds_remaining=300.0, yes_top_skew=0.0, no_top_skew=0.40),
        Regime.TREND_UP,
        orderbook_skew=cfg,
    )
    assert bid_heavy.p_up > ask_heavy_no.p_up
    assert bid_heavy.component_probabilities["orderbook_skew"] > 0.5
    assert ask_heavy_no.component_probabilities["orderbook_skew"] < 0.5


def test_orderbook_skew_blocked_near_strike():
    cfg = OrderbookSkewConfig(
        ensemble_enabled=True,
        ensemble_max_seconds_remaining=540.0,
        min_skew=0.25,
        min_z_distance=1.5,
    )
    estimate = EnsembleProbabilityModel().estimate(
        _features(seconds_remaining=300.0, z_distance=0.2, yes_top_skew=0.40),
        Regime.TREND_UP,
        orderbook_skew=cfg,
    )
    assert "orderbook_skew" not in estimate.component_probabilities
