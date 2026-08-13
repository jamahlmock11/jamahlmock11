"""Tests for lag reversal score and entry gates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kalshi_bot.config import LagReversalConfig
from kalshi_bot.domain import (
    ContractSide,
    FeatureSnapshot,
    MarketSnapshot,
    ProbabilityEstimate,
    Regime,
    TrajectoryState,
)
from kalshi_bot.features.enriched import EnrichedFeatures
from kalshi_bot.features.microstructure import MicrostructureSnapshot
from kalshi_bot.features.price_action import PriceActionSnapshot
from kalshi_bot.features.temporal import TemporalSnapshot
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.strategies.lag_reversal import ReversalContextTracker, evaluate_lag_reversal
from kalshi_bot.strategies.reversal_score import ReversalTier, compute_reversal_score

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _features(**overrides) -> FeatureSnapshot:
    base = {
        "timestamp": NOW,
        "current_price": 65100.0,
        "strike": 65000.0,
        "seconds_remaining": 480.0,
        "changes": {15: 0.00005, 30: 0.00012, 60: 0.00035},
        "velocities": {5: 0.00002, 10: 0.00004},
        "acceleration": -2e-6,
        "short_trend": 0.0004,
        "medium_trend": 0.0005,
        "realized_vol": 0.35,
        "expected_remaining_move": 120.0,
        "z_distance_to_strike": 0.85,
        "mean_reversion_score": -0.2,
        "orderbook_imbalance": 0.15,
        "cross_venue_agreement": 0.72,
        "cross_venue_dispersion": 0.001,
        "data_completeness": 0.9,
        "sample_count": 40,
        "oldest_sample_age": 2.0,
        "rationale": "test",
        "trajectory": TrajectoryState.REVERSING_UP,
        "late_momentum_pattern": "fade",
        "late_momentum_drift": 0.55,
        "late_momentum_hammer": 0.40,
        "late_momentum_fade": 0.72,
        "late_momentum_finish_bias": -0.35,
        "late_momentum_summary": "fade after extension",
    }
    base.update(overrides)
    return FeatureSnapshot(**base)


def _enriched(**pa_overrides) -> EnrichedFeatures:
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
        **pa_overrides,
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


def _forecast(p_up: float = 0.57) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        p_up=p_up,
        p_down=1.0 - p_up,
        confidence=0.68,
        signal_agreement=0.72,
        component_probabilities={"ensemble": p_up},
        regime=Regime.REVERSAL_UP,
        raw_p_up=p_up,
    )


def _market(yes_ask: float = 0.74) -> MarketSnapshot:
    yes_bid = yes_ask - 0.01
    no_bid = 1.0 - yes_ask
    book = parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_bid:.4f}", "200"], [f"{yes_bid - 0.02:.4f}", "150"]],
                "no_dollars": [[f"{no_bid:.4f}", "200"], [f"{no_bid - 0.02:.4f}", "150"]],
            }
        },
        timestamp=NOW,
    )
    return MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="CF Benchmarks BRTI",
        strike=65000.0,
        expiration=NOW + timedelta(minutes=8),
        open_time=NOW - timedelta(minutes=7),
        reference="BRTI",
        orderbook=book,
    )


def test_user_example_reversal_score_tier_and_side():
    assessment = compute_reversal_score(
        _features(),
        _enriched(),
        _forecast(0.57),
        market_yes_poll=0.74,
        regime=Regime.REVERSAL_UP,
        seconds_remaining=480.0,
        prior_p_up=0.84,
    )
    assert assessment.reversal_side is ContractSide.NO
    assert assessment.score >= 70.0
    assert assessment.tier in {ReversalTier.CANDIDATE, ReversalTier.STRONG}
    assert assessment.kalshi_lag_on_reversal_side > 0.10


def test_score_below_threshold_does_not_trade():
    cfg = LagReversalConfig(enabled=True, min_entry_score=95.0)
    result = evaluate_lag_reversal(
        _market(),
        features=_features(late_momentum_fade=0.1, z_distance_to_strike=0.1),
        enriched=_enriched(),
        forecast=_forecast(0.52),
        regime=Regime.RANGE,
        cfg=cfg,
        seconds_remaining=480.0,
    )
    assert result.signal is None


def test_lag_reversal_enters_when_score_and_edge_pass():
    tracker = ReversalContextTracker()
    tracker.record("KXBTC15M-TEST", 0.84)
    cfg = LagReversalConfig(enabled=True, min_entry_score=65.0, min_edge=0.10)
    result = evaluate_lag_reversal(
        _market(yes_ask=0.74),
        features=_features(),
        enriched=_enriched(),
        forecast=_forecast(0.57),
        regime=Regime.REVERSAL_UP,
        cfg=cfg,
        seconds_remaining=480.0,
        tracker=tracker,
    )
    assert result.signal is not None
    assert result.signal.strategy == "lag_reversal"
    assert result.signal.side is ContractSide.NO
    assert result.signal.edge >= 0.10
