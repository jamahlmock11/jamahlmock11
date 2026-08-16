from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.domain import ContractSide, FeatureSnapshot, ProbabilityEstimate, Regime, TrajectoryState
from kalshi_bot.execution.position_reversal import (
    PositionReversalConfig,
    evaluate_position_reversal,
)
from kalshi_bot.execution.stop_loss import evaluate_position_exit
from kalshi_bot.domain import BenchmarkQuote, GateFailure, MarketPosition, MarketSnapshot
from kalshi_bot.market.orderbook import parse_orderbook_fp

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def features(
    *,
    seconds_remaining: float = 150.0,
    current_price: float = 65_100.0,
    strike: float = 65_000.0,
    z_distance: float = 1.2,
    short_trend: float = 0.0003,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        timestamp=NOW,
        current_price=current_price,
        strike=strike,
        seconds_remaining=seconds_remaining,
        changes={5: short_trend, 10: short_trend, 15: short_trend},
        velocities={5: short_trend / 5, 10: short_trend / 10},
        acceleration=0.0,
        short_trend=short_trend,
        medium_trend=short_trend,
        realized_vol=0.65,
        expected_remaining_move=80,
        z_distance_to_strike=z_distance,
        mean_reversion_score=0.0,
        orderbook_imbalance=0.0,
        cross_venue_agreement=0.9,
        cross_venue_dispersion=0.0002,
        data_completeness=1.0,
        trajectory=TrajectoryState.ACCELERATING_UP,
        sample_count=100,
        oldest_sample_age=100,
    )


def forecast(p_up: float) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        p_up=p_up,
        p_down=1 - p_up,
        confidence=0.7,
        signal_agreement=0.7,
        component_probabilities={"brti_settlement_core": p_up},
        regime=Regime.TREND_UP,
        raw_p_up=p_up,
    )


def test_reversal_triggers_when_wrong_side_with_little_time_left():
    assessment = evaluate_position_reversal(
        position_side=ContractSide.YES,
        features=features(
            seconds_remaining=120.0,
            current_price=64_950.0,
            z_distance=-0.8,
            short_trend=-0.0004,
        ),
        forecast=forecast(0.38),
        cfg=PositionReversalConfig(),
    )
    assert assessment.should_reverse
    assert "wrong side" in assessment.reason or "below" in assessment.summary


def test_reversal_holds_when_path_still_supports_position():
    assessment = evaluate_position_reversal(
        position_side=ContractSide.YES,
        features=features(seconds_remaining=240.0, z_distance=1.5, short_trend=0.0004),
        forecast=forecast(0.62),
        cfg=PositionReversalConfig(),
    )
    assert not assessment.should_reverse


def test_reversal_skips_early_contract_when_outside_late_window():
    """Weak path signals must not exit with 8+ minutes left (outside reversal window)."""
    assessment = evaluate_position_reversal(
        position_side=ContractSide.YES,
        features=features(
            seconds_remaining=498.0,
            current_price=62_917.68,
            strike=62_920.65,
            z_distance=-0.18,
            short_trend=0.0,
        ),
        forecast=forecast(0.485),
        cfg=PositionReversalConfig(
            window_seconds=300.0,
            min_hold_probability=0.50,
            late_hold_probability=0.62,
            min_z_support=-0.30,
        ),
    )
    assert not assessment.should_reverse
    assert "held path intact" in assessment.reason


def test_position_exit_uses_reversal_signal():
    book = parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.5500", "10"]],
                "no_dollars": [["0.4300", "10"]],
            }
        },
        timestamp=NOW,
    )
    market = MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="BRTI",
        strike=65_000,
        expiration=NOW + timedelta(seconds=120),
        open_time=NOW - timedelta(minutes=10),
        reference="BRTI",
        orderbook=book,
        current_position=MarketPosition(
            side=ContractSide.YES,
            quantity=1,
            average_price=0.55,
            opened_at=NOW - timedelta(seconds=180),
        ),
    )
    signal = evaluate_position_exit(
        market=market,
        position=market.current_position,
        forecast=forecast(0.35),
        features=features(
            seconds_remaining=120.0,
            current_price=64_940.0,
            z_distance=-0.9,
            short_trend=-0.0005,
        ),
        failures=(),
        predicted_side=ContractSide.NO,
        quantity=1,
        stop_loss_fraction=0.55,
        min_hold_seconds=60,
        position_reversal=PositionReversalConfig(),
        now=NOW,
    )
    assert signal is not None
    assert signal.trigger == "position_reversal"
