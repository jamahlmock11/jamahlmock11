"""Tests for intelligence modules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.domain import FeatureSnapshot, TrajectoryState
from kalshi_bot.intelligence.kill_switch import ConfidenceKillSwitch
from kalshi_bot.intelligence.manipulation import ManipulationDetector
from kalshi_bot.intelligence.signals import compute_technical_signals
from kalshi_bot.learning.signal_weights import SignalWeightTracker
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.models.monte_carlo import simulate_finish_probability
from kalshi_bot.models.strike_gravity import assess_strike_gravity
from kalshi_bot.models.trading_regime import classify_trading_regime, TradingRegimeKind
from kalshi_bot.execution.risk import kelly_notional_usd
from kalshi_bot.domain import Regime

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def features(**kwargs):
    defaults = {
        "timestamp": NOW,
        "current_price": 65020.0,
        "strike": 65000.0,
        "seconds_remaining": 600.0,
        "changes": {5: 0.0002, 10: 0.0003, 15: 0.0004, 30: 0.0005, 60: 0.0006},
        "velocities": {5: 0.00004, 10: 0.00003, 15: 0.000026},
        "acceleration": 0.000001,
        "short_trend": 0.0003,
        "medium_trend": 0.0006,
        "realized_vol": 0.65,
        "expected_remaining_move": 80.0,
        "z_distance_to_strike": 0.25,
        "mean_reversion_score": -0.1,
        "orderbook_imbalance": 0.1,
        "cross_venue_agreement": 0.9,
        "cross_venue_dispersion": 0.001,
        "data_completeness": 0.9,
        "trajectory": TrajectoryState.ACCELERATING_UP,
        "sample_count": 50,
        "oldest_sample_age": 300.0,
    }
    defaults.update(kwargs)
    return FeatureSnapshot(**defaults)


def book():
    yes_ask = 0.52
    no_bid = 1.0 - yes_ask
    yes_bid = yes_ask - 0.02
    return parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_bid:.4f}", "100"]],
                "no_dollars": [[f"{no_bid:.4f}", "100"]],
            }
        },
        timestamp=NOW,
    )


def test_kill_switch_halts_on_poor_accuracy():
    ks = ConfidenceKillSwitch(window_size=25, halt_accuracy=0.55, min_samples=25)
    for _ in range(20):
        ks.record_outcome(False)
    for _ in range(5):
        ks.record_outcome(True)
    assert ks.halted
    assert "kill switch" in ks.halt_reason.lower()


def test_kill_switch_recovers():
    ks = ConfidenceKillSwitch(window_size=25, halt_accuracy=0.55, recovery_accuracy=0.60, min_samples=25)
    for _ in range(25):
        ks.record_outcome(False)
    assert ks.halted
    ks._outcomes.clear()
    for _ in range(25):
        ks.record_outcome(True)
    ks._evaluate()
    assert not ks.halted


def test_signal_weight_update():
    tracker = SignalWeightTracker()
    tracker.records["ema"].correct = 61
    tracker.records["ema"].total = 100
    tracker.records["rsi"].correct = 48
    tracker.records["rsi"].total = 100
    new = tracker.update_weights()
    assert new["ema"] > new["rsi"]


def test_monte_carlo_returns_probabilities():
    result = simulate_finish_probability(features(), paths=1000, seed=42)
    assert 0.0 < result.p_up < 1.0
    assert result.p_down == 1.0 - result.p_up
    assert result.paths_simulated == 1000


def test_strike_gravity_above_strike():
    assessment = assess_strike_gravity(features(current_price=65100.0))
    assert assessment.distance_to_strike > 0
    assert assessment.finish_probability_up > 0.5


def test_trading_regime_trending():
    regime = classify_trading_regime(features(), Regime.TREND_UP)
    assert regime.kind == TradingRegimeKind.TRENDING
    assert regime.signal_weights["ema"] == 0.40


def test_technical_signals():
    signals = compute_technical_signals(features(), book())
    probs = signals.as_probabilities()
    assert set(probs.keys()) == {"ema", "rsi", "vwap", "bollinger", "orderbook", "news"}
    assert all(0.0 <= v <= 1.0 for v in probs.values())


def test_manipulation_detector_no_history():
    detector = ManipulationDetector()
    assessment = detector.assess(book())
    assert not assessment.detected
    assert assessment.confidence_penalty == 0.0


def test_kelly_tier_sizing():
    assert kelly_notional_usd(0.03, 100.0) == 5.0
    assert kelly_notional_usd(0.08, 100.0) == 9.0
    assert kelly_notional_usd(0.15, 100.0) == 14.0
    assert kelly_notional_usd(0.20, 100.0) == 20.0
    assert kelly_notional_usd(0.25, 10.0) == 10.0
