"""Tests for advanced 15m bot features."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kalshi_bot.domain import FeatureSnapshot, Regime, TrajectoryState
from kalshi_bot.features.enriched import EnrichedFeatureEngine
from kalshi_bot.features.microstructure import MicrostructureTracker
from kalshi_bot.features.price_action import compute_price_action
from kalshi_bot.features.temporal import compute_temporal, minute_bucket
from kalshi_bot.intelligence.model_agreement import assess_model_agreement
from kalshi_bot.intelligence.trade_quality import assess_trade_quality
from kalshi_bot.learning.pattern_matcher import PatternMatcher
from kalshi_bot.learning.trade_recorder import TradeRecorder
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.models.ensemble import EnsembleProbabilityModel
from kalshi_bot.domain import MarketSnapshot, OrderBookSnapshot, ProbabilityEstimate

NOW = datetime(2026, 8, 8, 14, 30, tzinfo=timezone.utc)


def _features(**kwargs):
    defaults = {
        "timestamp": NOW,
        "current_price": 65020.0,
        "strike": 65000.0,
        "seconds_remaining": 420.0,
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


def _book():
    yes_ask = 0.52
    no_bid = 1.0 - yes_ask
    yes_bid = yes_ask - 0.02
    return parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_bid:.4f}", "100"], [f"{yes_bid - 0.01:.4f}", "50"]],
                "no_dollars": [[f"{no_bid:.4f}", "100"], [f"{no_bid - 0.01:.4f}", "50"]],
            }
        },
        timestamp=NOW,
    )


def _market(book: OrderBookSnapshot | None = None) -> MarketSnapshot:
    return MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="open",
        rules="BRTI",
        strike=65000.0,
        expiration=NOW + timedelta(minutes=7),
        open_time=NOW - timedelta(minutes=8),
        reference="BRTI",
        orderbook=book or _book(),
    )


def test_microstructure_tracker():
    tracker = MicrostructureTracker()
    features = _features()
    book = _book()
    snap = tracker.compute(book, features)
    assert -1.0 <= snap.bid_ask_imbalance <= 1.0
    assert snap.depth_top10_total > 0
    assert 0 <= snap.liquidity_score <= 100


def test_price_action_momentum():
    features = _features()
    pa = compute_price_action(features, Regime.TREND_UP)
    assert pa.momentum_15s != 0.0
    assert pa.momentum_30s != 0.0
    assert pa.momentum_60s != 0.0


def test_temporal_features():
    market = _market()
    temporal = compute_temporal(market, NOW)
    assert temporal.minutes_until_expiration == 7.0
    assert temporal.minute_bucket == "5-7m"
    assert temporal.market_session in {"US", "EUROPE", "OVERLAP", "ASIA", "OFF_HOURS"}


def test_model_agreement():
    features = _features()
    engine = EnrichedFeatureEngine()
    market = _market()
    regime = Regime.TREND_UP
    enriched = engine.compute(features, market, regime, now=NOW)
    forecast = EnsembleProbabilityModel().estimate(features, regime)
    agreement = assess_model_agreement(forecast, features, enriched, regime)
    assert 0.0 <= agreement.agreement <= 1.0
    assert len(agreement.votes) >= 4


def test_trade_quality_skip_mediocre():
    features = _features()
    engine = EnrichedFeatureEngine()
    market = _market()
    regime = Regime.CHAOTIC_UNSTABLE
    enriched = engine.compute(features, market, regime, now=NOW)
    forecast = ProbabilityEstimate(
        p_up=0.55,
        p_down=0.45,
        confidence=0.55,
        signal_agreement=0.50,
        component_probabilities={},
        regime=regime,
        raw_p_up=0.55,
    )
    agreement = assess_model_agreement(forecast, features, enriched, regime)
    pattern = PatternMatcher().match(features, enriched, regime)
    tq = assess_trade_quality(
        forecast=forecast,
        features=features,
        market=market,
        enriched=enriched,
        model_agreement=agreement,
        pattern_match=pattern,
        edge=0.15,
        regime=regime,
    )
    assert tq.recommendation == "SKIP"


def test_trade_quality_execute_strong():
    features = _features()
    engine = EnrichedFeatureEngine()
    market = _market()
    regime = Regime.TREND_UP
    enriched = engine.compute(features, market, regime, now=NOW)
    forecast = EnsembleProbabilityModel().estimate(features, regime)
    agreement = assess_model_agreement(forecast, features, enriched, regime)
    pattern = PatternMatcher().match(features, enriched, regime)
    tq = assess_trade_quality(
        forecast=forecast,
        features=features,
        market=market,
        enriched=enriched,
        model_agreement=agreement,
        pattern_match=pattern,
        edge=0.28,
        regime=regime,
        min_quality_score=50.0,
        max_dnt_score=60.0,
    )
    assert tq.trade_quality_score >= 50.0


def test_trade_recorder(tmp_path):
    recorder = TradeRecorder(db_path=tmp_path / "learning.db")
    recorder.record_entry(
        ticker="KXBTC15M-X",
        features={"momentum": 0.001},
        prediction=0.62,
        confidence=0.70,
        edge=0.25,
        action="BUY_UP",
        reason="test",
    )
    recorder.record_outcome("KXBTC15M-X", outcome=1.0, pnl=0.75)
    summary = recorder.summary()
    assert summary["resolved_records"] == 1
    rows = recorder.training_rows()
    assert len(rows) == 1
    assert rows[0]["outcome"] == 1.0


def test_minute_bucket():
    assert minute_bucket(30) == "0-1m"
    assert minute_bucket(240) == "3-5m"
    assert minute_bucket(480) == "7-10m"
    assert minute_bucket(720) == "10-15m"
