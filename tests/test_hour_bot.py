"""Tests for 1-hour edge tiers, dynamic edge, and decision logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.config import HourEdgeConfig, HourStrategyConfig
from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    FeatureSnapshot,
    MarketSnapshot,
    ProbabilityEstimate,
    Regime,
    TrajectoryState,
    TradeTier,
)
from kalshi_bot.hour.decision import HourDecisionConfig, HourDecisionEngine
from kalshi_bot.hour.edge_engine import assess_edge, classify_trade_tier, required_edge
from kalshi_bot.hour.trend_engine import classify_trend
from kalshi_bot.hour.volatility_model import analyze_volatility
from kalshi_bot.market.orderbook import parse_orderbook_fp

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
HOUR_CFG = HourStrategyConfig()
EDGE_CFG = HourEdgeConfig()


def book(yes_ask: float = 0.55):
    yes_bid = yes_ask - 0.02
    no_bid = 1.0 - yes_ask
    return parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_bid:.4f}", "100"]],
                "no_dollars": [[f"{no_bid:.4f}", "100"]],
            }
        },
        timestamp=NOW,
    )


def hour_market(yes_ask: float = 0.55, *, minutes_remaining: float = 15.0):
    return MarketSnapshot(
        ticker="KXBTCD-26AUG081200-T65000",
        status="active",
        rules="60 second average of CF Benchmarks BRTI",
        strike=65_000,
        expiration=NOW + timedelta(minutes=minutes_remaining),
        open_time=NOW - timedelta(minutes=60 - minutes_remaining),
        reference="CME CF Bitcoin Real Time Index (BRTI)",
        orderbook=book(yes_ask),
    )


def hour_features():
    changes = {
        5: 0.0002,
        15: 0.0003,
        30: 0.0004,
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
        seconds_remaining=900,
        changes=changes,
        velocities={5: 0.00004},
        acceleration=0.000001,
        short_trend=0.0003,
        medium_trend=0.0006,
        realized_vol=0.55,
        expected_remaining_move=200,
        z_distance_to_strike=0.1,
        mean_reversion_score=-0.1,
        orderbook_imbalance=0.1,
        cross_venue_agreement=0.85,
        cross_venue_dispersion=0.0002,
        data_completeness=0.9,
        trajectory=TrajectoryState.ACCELERATING_UP,
        sample_count=500,
        oldest_sample_age=3600,
    )


def benchmark():
    return BenchmarkQuote(
        price=65_020,
        timestamp=NOW,
        source="CME CF Bitcoin Real Time Index (BRTI)",
        primary=True,
        is_live=True,
    )


def forecast(p_up: float):
    return ProbabilityEstimate(
        p_up=p_up,
        p_down=1 - p_up,
        confidence=0.75,
        signal_agreement=0.82,
        component_probabilities={"terminal": p_up},
        regime=Regime.TREND_UP,
        raw_p_up=p_up,
    )


def make_engine(allow_proxy: bool = True) -> HourDecisionEngine:
    return HourDecisionEngine(
        HourDecisionConfig(
            hour=HOUR_CFG,
            edge=EDGE_CFG,
            allow_proxy_data=allow_proxy,
        )
    )


def test_model_65_price_55_buys_when_gates_pass():
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
    engine = make_engine()
    decision = engine.decide(
        hour_market(0.48),
        forecast(0.65),
        features,
        benchmark(),
        trend,
        vol,
        Regime.TREND_UP,
        0.8,
        now=NOW,
    )
    assert decision.action is DecisionAction.BUY_UP
    assert decision.edge is not None and decision.edge >= 0.10


def test_model_65_price_56_no_trade_when_required_edge_10():
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
    engine = make_engine()
    decision = engine.decide(
        hour_market(0.56),
        forecast(0.65),
        features,
        benchmark(),
        trend,
        vol,
        Regime.TREND_UP,
        0.8,
        now=NOW,
    )
    assert decision.action is DecisionAction.NO_TRADE


def test_model_75_price_55_a_plus_trade():
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
    engine = make_engine()
    decision = engine.decide(
        hour_market(0.54),
        forecast(0.75),
        features,
        benchmark(),
        trend,
        vol,
        Regime.TREND_UP,
        0.85,
        now=NOW,
    )
    assert decision.action is DecisionAction.BUY_UP
    assert decision.trade_tier is TradeTier.A_PLUS


def test_dynamic_edge_increases_near_expiration():
    features = hour_features()
    trend = classify_trend(dict(features.changes))
    vol = analyze_volatility(
        current_price=features.current_price,
        strike=features.strike,
        seconds_remaining=45,
        realized_vol=features.realized_vol,
        changes=dict(features.changes),
        prices=[65000, 65010, 65020],
        timestamps_span=3600,
    )
    req = required_edge(
        seconds_remaining=45,
        volatility=vol,
        spread=0.04,
        depth=50,
        confidence=0.75,
        agreement=0.8,
        regime=Regime.TREND_UP,
        z_distance=0.1,
        trend=trend,
        model_stability=0.8,
        hour_cfg=HOUR_CFG,
        edge_cfg=EDGE_CFG,
        entry_timing=__import__(
            "kalshi_bot.domain", fromlist=["EntryTiming"]
        ).EntryTiming.LATE,
    )
    assert req >= EDGE_CFG.preferred_edge


def test_tier_classification():
    assert classify_trade_tier(
        0.21,
        0.75,
        0.8,
        edge_cfg=EDGE_CFG,
        hour_cfg=HOUR_CFG,
        spread=0.04,
        depth=10,
    ) is TradeTier.A_PLUS
    assert classify_trade_tier(
        0.16,
        0.7,
        0.7,
        edge_cfg=EDGE_CFG,
        hour_cfg=HOUR_CFG,
        spread=0.04,
        depth=10,
    ) is TradeTier.A
    assert classify_trade_tier(
        0.11,
        0.7,
        0.7,
        edge_cfg=EDGE_CFG,
        hour_cfg=HOUR_CFG,
        spread=0.04,
        depth=10,
    ) is TradeTier.B
    assert classify_trade_tier(
        0.06,
        0.7,
        0.7,
        edge_cfg=EDGE_CFG,
        hour_cfg=HOUR_CFG,
        spread=0.04,
        depth=10,
    ) is TradeTier.NONE


def test_assess_edge_below_minimum():
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
    assessment = assess_edge(
        up_probability=0.64,
        down_probability=0.36,
        up_executable=0.57,
        down_executable=0.45,
        seconds_remaining=features.seconds_remaining,
        volatility=vol,
        yes_spread=0.04,
        no_spread=0.04,
        yes_depth=50,
        no_depth=50,
        confidence=0.7,
        agreement=0.75,
        regime=Regime.TREND_UP,
        z_distance=0.1,
        trend=trend,
        model_stability=0.75,
        hour_cfg=HOUR_CFG,
        edge_cfg=EDGE_CFG,
    )
    assert assessment.up_edge == pytest.approx(0.07, abs=0.01)
    assert assessment.trade_tier is TradeTier.NONE


def test_time_window_blocks_entries_before_last_20_minutes():
    engine = make_engine()
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
    decision = engine.decide(
        hour_market(0.54, minutes_remaining=35),
        forecast(0.65),
        features,
        benchmark(),
        trend,
        vol,
        Regime.TREND_UP,
        0.8,
        now=NOW,
    )
    assert decision.action is DecisionAction.NO_TRADE
    assert any(f.gate == "time_window" for f in decision.gate_failures)


def test_1h_yaml_loads_without_validation_error():
    from kalshi_bot.config import load_yaml_config

    cfg = load_yaml_config("config/1h.yaml")
    assert cfg.horizon == "1h"
    assert cfg.hour.series_ticker == "KXBTCD"
    assert cfg.strategy.target_edge >= 0.20
    assert cfg.strategy.final_min_edge >= 0.20
    assert cfg.hour_edge.preferred_edge == pytest.approx(0.15)
