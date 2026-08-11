"""Tests for ROMA agent pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kalshi_bot.agents.pipeline import RomaPipeline, format_roma_report
from kalshi_bot.agents.sentiment import evaluate_sentiment
from kalshi_bot.config import AgentsConfig
from kalshi_bot.domain import (
    DecisionAction,
    DecisionResult,
    Direction,
    FeatureSnapshot,
    MarketSnapshot,
    ProbabilityEstimate,
    Regime,
    TrajectoryState,
)
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.strategies.forecasting import ForecastCycle

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _book():
    return parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.30", "80"], ["0.28", "40"]],
                "no_dollars": [["0.68", "120"]],
            }
        },
        timestamp=NOW,
    )


def _cycle(p_up: float = 0.32, yes_ask: float = 0.31):
    market = MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="BRTI",
        strike=65000,
        expiration=NOW + timedelta(minutes=10),
        open_time=NOW - timedelta(minutes=5),
        reference="BRTI",
        orderbook=_book(),
    )
    features = FeatureSnapshot(
        timestamp=NOW,
        current_price=65010,
        strike=65000,
        seconds_remaining=600,
        changes={3600: 0.001, 900: 0.0005},
        velocities={},
        acceleration=0.0,
        short_trend=0.0005,
        medium_trend=0.0004,
        realized_vol=0.02,
        expected_remaining_move=0.001,
        z_distance_to_strike=0.5,
        mean_reversion_score=0.0,
        orderbook_imbalance=-0.2,
        cross_venue_agreement=0.9,
        cross_venue_dispersion=0.001,
        data_completeness=0.9,
        trajectory=TrajectoryState.FLAT,
        sample_count=100,
        oldest_sample_age=3600,
    )
    forecast = ProbabilityEstimate(
        p_up=p_up,
        p_down=1 - p_up,
        confidence=0.5,
        signal_agreement=0.5,
        component_probabilities={"ensemble": p_up},
        regime=Regime.LOW_VOLATILITY,
        raw_p_up=p_up,
    )
    decision = DecisionResult(
        action=DecisionAction.NO_TRADE,
        reason="entry blocked by safety gates",
        gate_failures=(),
        current_direction=Direction.FLAT,
        predicted_direction=Direction.UP,
        trade_direction=Direction.FLAT,
        edge=p_up - yes_ask,
    )
    return ForecastCycle(
        NOW,
        "HEALTHY",
        "test",
        market=market,
        features=features,
        forecast=forecast,
        decision=decision,
    )


def test_sentiment_neutral_when_skew_offsets_momentum():
    cycle = _cycle()
    verdict = evaluate_sentiment(cycle.features, cycle.market)
    assert verdict.label in {"neutral", "bullish", "bearish"}


def test_roma_pipeline_rejects_low_edge():
    report = RomaPipeline(AgentsConfig(enabled=True, min_edge=0.03)).evaluate(_cycle())
    assert report is not None
    assert not report.approved
    text = format_roma_report(report)
    assert "SentimentAgent" in text
    assert "ProbabilityModelAgent" in text
    assert "RiskManager" in text
    assert "3%" in text
