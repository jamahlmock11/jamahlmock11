"""Tests for 1-hour reversal strategy scoring and decisions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.config import AppConfig, HourReversalConfig, load_yaml_config
from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    Direction,
    FeatureSnapshot,
    MarketSnapshot,
    ProbabilityEstimate,
    Regime,
    TrajectoryState,
)
from kalshi_bot.hour.reversal_decision import HourReversalDecisionEngine
from kalshi_bot.hour.reversal_engine import ReversalTier, assess_reversal
from kalshi_bot.hour.reversal_state import ReversalStateTracker
from kalshi_bot.hour.trend_engine import TrendSnapshot, classify_trend
from kalshi_bot.hour.volatility_model import analyze_volatility
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.market.poll_alignment import market_poll_snapshot

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def book(yes_ask: float = 0.74):
    yes_bid = yes_ask - 0.02
    no_bid = 1.0 - yes_ask
    no_ask = no_bid + 0.02
    return parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_bid:.4f}", "100"]],
                "no_dollars": [[f"{no_bid:.4f}", "100"]],
            }
        },
        timestamp=NOW,
    )


def hour_market(yes_ask: float = 0.74, *, minutes_remaining: float = 8.0):
    return MarketSnapshot(
        ticker="KXBTCD-26AUG121200-T65000",
        status="active",
        rules="60 second average of CF Benchmarks BRTI",
        strike=65_000,
        expiration=NOW + timedelta(minutes=minutes_remaining),
        open_time=NOW - timedelta(minutes=60 - minutes_remaining),
        reference="CME CF Bitcoin Real Time Index (BRTI)",
        orderbook=book(yes_ask),
    )


def hour_features(*, trajectory: TrajectoryState = TrajectoryState.REVERSING_DOWN):
    changes = {
        5: -0.0004,
        15: -0.0003,
        30: 0.0002,
        60: 0.0005,
        180: 0.0006,
        300: 0.0007,
        600: 0.0008,
        900: 0.0009,
        1800: 0.001,
        3600: 0.0011,
    }
    return FeatureSnapshot(
        timestamp=NOW,
        current_price=65_020,
        strike=65_000,
        seconds_remaining=480,
        changes=changes,
        velocities={5: -0.00004},
        acceleration=-0.00001,
        short_trend=-0.0003,
        medium_trend=0.0005,
        realized_vol=0.55,
        expected_remaining_move=120,
        z_distance_to_strike=0.4,
        mean_reversion_score=0.2,
        orderbook_imbalance=-0.12,
        cross_venue_agreement=0.82,
        cross_venue_dispersion=0.001,
        data_completeness=0.9,
        trajectory=trajectory,
        sample_count=400,
        oldest_sample_age=3600,
        late_momentum_pattern="fade",
        late_momentum_fade=0.6,
        late_momentum_hammer=0.4,
        late_momentum_summary="fade · DOWN push",
    )


def hour_forecast(*, p_up: float = 0.57):
    return ProbabilityEstimate(
        p_up=p_up,
        p_down=1.0 - p_up,
        confidence=0.92,
        signal_agreement=0.68,
        component_probabilities={"ensemble": p_up},
        regime=Regime.REVERSAL_DOWN,
        raw_p_up=p_up,
        notes=("reversal test",),
    )


def benchmark_quote() -> BenchmarkQuote:
    return BenchmarkQuote(
        price=65_020,
        timestamp=NOW,
        source="BRTI",
        primary=True,
        is_live=True,
    )


def test_reversal_score_tiers():
    cfg = HourReversalConfig()
    assert cfg.watch_score == 50
    assert cfg.min_reversal_score == 70
    assert cfg.strong_reversal_score == 85


def test_reversal_example_setup_scores_candidate():
    cfg = HourReversalConfig()
    features = hour_features()
    trend = classify_trend(dict(features.changes))
    vol = analyze_volatility(
        current_price=features.current_price,
        strike=features.strike,
        seconds_remaining=features.seconds_remaining,
        realized_vol=features.realized_vol,
        changes=dict(features.changes),
        prices=[65000, 65010, 65020],
        timestamps_span=3600,
    )
    poll = market_poll_snapshot(book(0.74))
    tracker = ReversalStateTracker()
    state = tracker.get("KXBTCD-TEST")
    state.established = True
    state.initial_direction = Direction.UP
    state.peak_model_prob = 0.84
    state.peak_kalshi_poll = 0.78
    assessment = assess_reversal(
        features=features,
        forecast=hour_forecast(p_up=0.57),
        trend=trend,
        vol=vol,
        poll=poll,
        state=state,
        cfg=cfg,
    )
    assert assessment.tier in {ReversalTier.CANDIDATE, ReversalTier.STRONG, ReversalTier.WATCH}
    assert assessment.reversal_direction is not None


def test_reversal_decision_enters_no_on_lagged_kalshi():
    cfg = AppConfig()
    cfg.hour_reversal = HourReversalConfig(min_reversal_score=60, min_entry_edge=0.10)
    engine = HourReversalDecisionEngine(cfg)
    market = hour_market(yes_ask=0.74)
    features = hour_features()
    trend = classify_trend(dict(features.changes))
    vol = analyze_volatility(
        current_price=features.current_price,
        strike=features.strike,
        seconds_remaining=features.seconds_remaining,
        realized_vol=features.realized_vol,
        changes=dict(features.changes),
        prices=[65000, 65010, 65020],
        timestamps_span=3600,
    )
    poll = market_poll_snapshot(market.orderbook)
    state = engine.state_tracker.get(market.ticker)
    state.established = True
    state.initial_direction = Direction.UP
    state.peak_model_prob = 0.84
    state.peak_kalshi_poll = 0.78
    reversal = assess_reversal(
        features=features,
        forecast=hour_forecast(p_up=0.57),
        trend=trend,
        vol=vol,
        poll=poll,
        state=state,
        cfg=cfg.hour_reversal,
    )
    reversal = replace(
        reversal,
        score=78.0,
        tier=ReversalTier.CANDIDATE,
        confirmed=True,
        confirmation_reason="reversal confirmed",
    )
    decision = engine.decide(
        market,
        hour_forecast(p_up=0.57),
        features,
        benchmark_quote(),
        trend=trend,
        vol=vol,
        poll=poll,
        reversal=reversal,
        now=NOW,
    )
    assert decision.action is DecisionAction.BUY_DOWN
    assert decision.selected_side is ContractSide.NO
    assert decision.edge is not None
    assert decision.edge > 0.10
    assert decision.entry_strategy == "reversal"


def test_1h_yaml_loads_reversal_config():
    cfg = load_yaml_config("config/1h.yaml")
    assert cfg.horizon == "1h"
    assert cfg.hour_reversal.enabled is True
    assert cfg.hour_reversal.min_reversal_score == pytest.approx(70)
    assert cfg.longshot.enabled is False
