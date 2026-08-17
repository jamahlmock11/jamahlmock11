"""Tests for closed-loop learning from trade outcomes."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kalshi_bot.domain import ContractSide, FeatureSnapshot, Regime, TrajectoryState
from kalshi_bot.features.enriched import EnrichedFeatures
from kalshi_bot.features.microstructure import MicrostructureSnapshot
from kalshi_bot.features.price_action import PriceActionSnapshot
from kalshi_bot.features.temporal import TemporalSnapshot
from kalshi_bot.journal import TradeJournal
from kalshi_bot.learning.outcomes import record_round_trip_learning, round_trip_outcome
from kalshi_bot.learning.pattern_matcher import PatternMatcher
from kalshi_bot.learning.signal_weights import SignalWeightTracker
from kalshi_bot.learning.trade_recorder import TradeRecorder

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _features() -> FeatureSnapshot:
    return FeatureSnapshot(
        timestamp=NOW,
        current_price=65_020,
        strike=65_000,
        seconds_remaining=480.0,
        changes={5: 0.0002},
        velocities={5: 0.00004},
        acceleration=0.0,
        short_trend=0.0003,
        medium_trend=0.0006,
        realized_vol=0.65,
        expected_remaining_move=80,
        z_distance_to_strike=1.2,
        mean_reversion_score=-0.1,
        orderbook_imbalance=0.1,
        yes_top_skew=0.2,
        cross_venue_agreement=0.9,
        cross_venue_dispersion=0.0002,
        data_completeness=1.0,
        trajectory=TrajectoryState.ACCELERATING_UP,
        sample_count=100,
        oldest_sample_age=100,
    )


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
        breakout_detected=False,
        fake_breakout=False,
        breakout_direction="UP",
    )
    micro = MicrostructureSnapshot(
        bid_ask_imbalance=0.1,
        depth_top10_yes=500.0,
        depth_top10_no=500.0,
        depth_top10_total=1000.0,
        whale_detected=False,
        whale_side=None,
        whale_size=0.0,
        cancellation_rate=0.0,
        new_order_pressure=0.0,
        spread_yes=0.02,
        spread_no=0.02,
        spread_trend=0.0,
        trade_velocity=0.5,
        liquidity_score=90.0,
    )
    temporal = TemporalSnapshot(
        minutes_until_expiration=8.0,
        day_of_week=4,
        hour_of_day=12,
        market_session="EUROPE",
        historical_win_rate=None,
        historical_sample_count=0,
        minute_bucket="7-10m",
    )
    return EnrichedFeatures(microstructure=micro, price_action=pa, temporal=temporal)


def test_round_trip_outcome_labels_yes_win():
    labels = round_trip_outcome(
        ticker="KXBTC15M-TEST",
        held_side=ContractSide.YES,
        pnl=0.08,
    )
    assert labels.round_trip_win is True
    assert labels.outcome == 1.0
    assert labels.actual_up is True


def test_round_trip_outcome_labels_no_loss():
    labels = round_trip_outcome(
        ticker="KXBTC15M-TEST",
        held_side=ContractSide.NO,
        pnl=-0.19,
    )
    assert labels.round_trip_win is False
    assert labels.outcome == 0.0
    assert labels.actual_up is True


def test_record_round_trip_learning_updates_stores(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.db"))
    recorder = TradeRecorder(db_path=tmp_path / "learning.db")
    patterns = PatternMatcher(
        journal_path=tmp_path / "journal.db",
        patterns_path=tmp_path / "patterns.json",
    )
    weights = SignalWeightTracker(minimum_samples=1)
    weights_path = tmp_path / "signal_weights.json"

    recorder.record_entry(
        ticker="KXBTC15M-TEST",
        features={"momentum": 0.001},
        prediction=0.58,
        confidence=0.7,
        edge=0.12,
        action="BUY_UP",
        reason="test entry",
    )
    patterns.save_entry(
        _features(),
        _enriched(),
        Regime.TREND_UP,
        ticker="KXBTC15M-TEST",
        prediction=0.58,
        confidence=0.7,
        edge=0.12,
        action="BUY_UP",
    )

    labels = record_round_trip_learning(
        ticker="KXBTC15M-TEST",
        held_side=ContractSide.YES,
        pnl=0.08,
        trade_recorder=recorder,
        pattern_matcher=patterns,
        journal=journal,
        signal_weights=weights,
        signal_weights_path=weights_path,
        features=_features(),
    )

    assert labels.round_trip_win is True
    assert recorder.summary()["resolved_records"] == 1
    stored = patterns._load_stored_patterns()
    assert stored[-1]["outcome"] == 1.0
    assert stored[-1]["pnl"] == pytest.approx(0.08)
    assert weights_path.exists()
