"""Tests for forecast alignment and rally contrarian entry gates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.config import PollConfig
from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    FeatureSnapshot,
    TrajectoryState,
)
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.strategies.decision import DecisionConfig, DecisionEngine

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _book(*, yes_ask: float = 0.55):
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


def _market(*, yes_ask: float = 0.55):
    from kalshi_bot.domain import MarketSnapshot

    return MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="BRTI",
        strike=65_000,
        expiration=NOW + timedelta(minutes=8),
        open_time=NOW - timedelta(minutes=7),
        reference="BRTI",
        orderbook=_book(yes_ask=yes_ask),
        current_position=None,
    )


def _features(
    *,
    current_price: float = 65_020.0,
    strike: float = 65_000.0,
    short_trend: float = 0.0003,
    seconds_remaining: float = 480.0,
):
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
        z_distance_to_strike=(current_price - strike) / 100,
        mean_reversion_score=0.0,
        orderbook_imbalance=0.0,
        cross_venue_agreement=0.9,
        cross_venue_dispersion=0.0002,
        data_completeness=1.0,
        trajectory=TrajectoryState.ACCELERATING_UP,
        sample_count=100,
        oldest_sample_age=300,
    )


def _forecast(*, p_up: float):
    from kalshi_bot.domain import ProbabilityEstimate, Regime

    return ProbabilityEstimate(
        p_up=p_up,
        p_down=1 - p_up,
        confidence=0.75,
        signal_agreement=0.70,
        component_probabilities={"terminal_distribution": p_up},
        regime=Regime.TREND_UP if p_up >= 0.5 else Regime.TREND_DOWN,
        raw_p_up=p_up,
    )


def _benchmark():
    return BenchmarkQuote(
        price=65_020.0,
        timestamp=NOW,
        source="BRTI",
        primary=True,
        is_live=True,
    )


def _engine(**overrides) -> DecisionEngine:
    base = dict(
        minimum_edge=0.10,
        target_edge=0.10,
        minimum_confidence=0.0,
        minimum_agreement=0.55,
        poll=PollConfig(mode="disabled"),
    )
    base.update(overrides)
    return DecisionEngine(DecisionConfig(**base))


def test_forecast_alignment_blocks_no_when_ensemble_favors_up():
    """Cheap NO book edge must not override a dominant UP ensemble forecast."""
    engine = _engine(
        require_forecast_alignment=True,
        forecast_alignment_min_probability=0.52,
        block_rally_contrarian_entries=False,
    )
    # YES expensive (low edge), NO cheap (high edge) but ensemble says UP 62%.
    decision = engine.decide(
        _market(yes_ask=0.72),
        _forecast(p_up=0.62),
        _features(current_price=65_050, short_trend=0.0001),
        _benchmark(),
        now=NOW,
    )
    assert decision.action is DecisionAction.NO_TRADE
    assert any(f.gate == "forecast_alignment" for f in decision.gate_failures)
    assert decision.selected_side is ContractSide.NO


def test_rally_gate_blocks_no_when_spot_above_strike_and_rallying():
    """NO entries must not fire while BTC is above strike and moving up."""
    engine = _engine(
        require_forecast_alignment=False,
        block_rally_contrarian_entries=True,
    )
    decision = engine.decide(
        _market(yes_ask=0.55),
        _forecast(p_up=0.48),
        _features(current_price=65_100, strike=65_000, short_trend=0.0004),
        _benchmark(),
        now=NOW,
    )
    assert decision.action is DecisionAction.NO_TRADE
    assert any(f.gate == "spot_momentum_alignment" for f in decision.gate_failures)


def test_aligned_yes_entry_allowed_when_ensemble_and_spot_agree():
    engine = _engine(
        require_forecast_alignment=True,
        block_rally_contrarian_entries=True,
    )
    decision = engine.decide(
        _market(yes_ask=0.40),
        _forecast(p_up=0.62),
        _features(current_price=65_050, short_trend=0.0003),
        _benchmark(),
        now=NOW,
    )
    assert decision.action is DecisionAction.BUY_UP
    assert not any(f.gate == "forecast_alignment" for f in decision.gate_failures)
