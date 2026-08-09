from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    FeatureSnapshot,
    MarketPosition,
    MarketSnapshot,
    ProbabilityEstimate,
    Regime,
    TrajectoryState,
)
from kalshi_bot.execution.stop_loss import (
    evaluate_position_exit,
    premium_loss_fraction,
    thesis_reversal_triggered,
)
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.strategies.decision import DecisionConfig, DecisionEngine

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def book(yes_bid: float = 0.48):
    return parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_bid:.4f}", "100"]],
                "no_dollars": [[f"{1.0 - yes_bid - 0.02:.4f}", "100"]],
            }
        },
        timestamp=NOW,
    )


def test_premium_loss_fraction():
    assert premium_loss_fraction(0.50, 0.275) == pytest.approx(0.45)
    assert premium_loss_fraction(0.50, 0.50) == 0.0


def test_stop_loss_triggers_at_45_percent_premium_loss():
    market = MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="BRTI",
        strike=65000,
        expiration=NOW + timedelta(minutes=5),
        open_time=NOW - timedelta(minutes=10),
        reference="BRTI",
        orderbook=book(yes_bid=0.27),
        current_position=MarketPosition(
            side=ContractSide.YES,
            quantity=1,
            average_price=0.50,
        ),
    )
    forecast = ProbabilityEstimate(
        p_up=0.55,
        p_down=0.45,
        confidence=0.7,
        signal_agreement=0.8,
        component_probabilities={"terminal": 0.55},
        regime=Regime.TREND_UP,
        raw_p_up=0.55,
    )
    signal = evaluate_position_exit(
        market=market,
        position=market.current_position,
        forecast=forecast,
        failures=(),
        predicted_side=ContractSide.YES,
        quantity=1,
        stop_loss_fraction=0.45,
    )
    assert signal is not None
    assert signal.trigger == "stop_loss"
    assert signal.premium_loss_fraction == pytest.approx(0.46, abs=0.02)


def test_thesis_reversal_exits_before_stop():
    market = MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="BRTI",
        strike=65000,
        expiration=NOW + timedelta(minutes=5),
        open_time=NOW - timedelta(minutes=10),
        reference="BRTI",
        orderbook=book(yes_bid=0.46),
        current_position=MarketPosition(
            side=ContractSide.YES,
            quantity=1,
            average_price=0.50,
        ),
    )
    forecast = ProbabilityEstimate(
        p_up=0.40,
        p_down=0.60,
        confidence=0.7,
        signal_agreement=0.8,
        component_probabilities={"terminal": 0.40},
        regime=Regime.TREND_UP,
        raw_p_up=0.40,
    )
    signal = evaluate_position_exit(
        market=market,
        position=market.current_position,
        forecast=forecast,
        failures=(),
        predicted_side=ContractSide.NO,
        quantity=1,
        stop_loss_fraction=0.45,
    )
    assert signal is not None
    assert signal.trigger == "thesis_reversal"


def test_minor_forecast_flip_does_not_trigger_thesis_exit():
    """49/51 noise should not exit a YES position when margin is 10pp."""
    market = MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="BRTI",
        strike=65000,
        expiration=NOW + timedelta(minutes=5),
        open_time=NOW - timedelta(minutes=10),
        reference="BRTI",
        orderbook=book(yes_bid=0.46),
        current_position=MarketPosition(
            side=ContractSide.YES,
            quantity=1,
            average_price=0.50,
        ),
    )
    forecast = ProbabilityEstimate(
        p_up=0.49,
        p_down=0.51,
        confidence=0.7,
        signal_agreement=0.8,
        component_probabilities={"terminal": 0.49},
        regime=Regime.TREND_UP,
        raw_p_up=0.49,
    )
    assert not thesis_reversal_triggered(
        market.current_position,
        forecast,
        margin=0.10,
    )
    signal = evaluate_position_exit(
        market=market,
        position=market.current_position,
        forecast=forecast,
        failures=(),
        predicted_side=ContractSide.NO,
        quantity=1,
        stop_loss_fraction=0.45,
        thesis_reversal_margin=0.10,
    )
    assert signal is None


def test_15m_decision_engine_exits_on_stop_loss():
    engine = DecisionEngine(
        DecisionConfig(
            stop_loss_fraction=0.45,
            quantity=1,
            allow_proxy_data=True,
        )
    )
    market = MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="CF Benchmarks BRTI",
        strike=65000,
        expiration=NOW + timedelta(minutes=5),
        open_time=NOW - timedelta(minutes=10),
        reference="BRTI",
        orderbook=book(yes_bid=0.27),
        current_position=MarketPosition(
            side=ContractSide.YES,
            quantity=1,
            average_price=0.50,
        ),
        valid=True,
    )
    decision = engine.decide(
        market,
        ProbabilityEstimate(
            p_up=0.55,
            p_down=0.45,
            confidence=0.7,
            signal_agreement=0.8,
            component_probabilities={"terminal": 0.55},
            regime=Regime.TREND_UP,
            raw_p_up=0.55,
        ),
        FeatureSnapshot(
            timestamp=NOW,
            current_price=65000,
            strike=65000,
            seconds_remaining=300,
            changes={5: 0.0001},
            velocities={5: 0.0001},
            acceleration=0.0,
            short_trend=0.0001,
            medium_trend=0.0001,
            realized_vol=0.5,
            expected_remaining_move=100,
            z_distance_to_strike=0.0,
            mean_reversion_score=0.0,
            orderbook_imbalance=0.0,
            cross_venue_agreement=0.9,
            cross_venue_dispersion=0.0001,
            data_completeness=0.9,
            trajectory=TrajectoryState.FLAT,
            sample_count=100,
            oldest_sample_age=300,
        ),
        BenchmarkQuote(
            price=65000,
            timestamp=NOW,
            source="CME CF Bitcoin Real Time Index (BRTI)",
            primary=True,
            is_live=True,
        ),
        now=NOW,
    )
    assert decision.action is DecisionAction.EXIT
    assert "stop loss" in decision.reason.lower()
