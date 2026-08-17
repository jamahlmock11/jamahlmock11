"""Tests for forecast-aligned side selection (no edge-based contrarian picks)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kalshi_bot.config import ForecastAlignmentConfig, PollConfig
from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    Direction,
    FeatureSnapshot,
    ProbabilityEstimate,
    Regime,
    TrajectoryState,
)
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.strategies.decision import DecisionConfig, DecisionEngine

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _book(*, yes_ask: float = 0.21):
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


def _market(*, yes_ask: float = 0.21):
    from kalshi_bot.domain import MarketSnapshot

    return MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="BRTI",
        strike=63_313.66,
        expiration=NOW + timedelta(minutes=8),
        open_time=NOW - timedelta(minutes=7),
        reference="BRTI",
        orderbook=_book(yes_ask=yes_ask),
        current_position=None,
    )


def _features(*, current_price: float = 63_292.16, short_trend: float = -0.0002):
    return FeatureSnapshot(
        timestamp=NOW,
        current_price=current_price,
        strike=63_313.66,
        seconds_remaining=480.0,
        changes={5: short_trend, 10: short_trend, 15: short_trend},
        velocities={5: short_trend / 5, 10: short_trend / 10},
        acceleration=0.0,
        short_trend=short_trend,
        medium_trend=short_trend,
        realized_vol=0.65,
        expected_remaining_move=80,
        z_distance_to_strike=(current_price - 63_313.66) / 100,
        mean_reversion_score=0.0,
        orderbook_imbalance=0.0,
        cross_venue_agreement=0.9,
        cross_venue_dispersion=0.0002,
        data_completeness=1.0,
        trajectory=TrajectoryState.ACCELERATING_DOWN,
        sample_count=100,
        oldest_sample_age=300,
    )


def _forecast(*, p_up: float = 0.38):
    return ProbabilityEstimate(
        p_up=p_up,
        p_down=1 - p_up,
        confidence=0.70,
        signal_agreement=0.68,
        component_probabilities={"terminal_distribution": p_up},
        regime=Regime.TREND_DOWN,
        raw_p_up=p_up,
    )


def _benchmark():
    return BenchmarkQuote(
        price=63_292.16,
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
        use_edge_based_side_pick=False,
        contrarian_fallback_enabled=False,
        aligned_edge_premium=0.0,
        forecast_alignment=ForecastAlignmentConfig(enabled=False),
        block_rally_contrarian_entries=True,
    )
    base.update(overrides)
    return DecisionEngine(DecisionConfig(**base))


def test_picks_down_when_model_and_kalshi_align_even_if_up_has_better_edge():
    # Kalshi ~79% DOWN (yes ask 21¢), model 62% DOWN, but YES is cheaper so edge favors UP.
    decision = _engine().decide(
        _market(yes_ask=0.21),
        _forecast(p_up=0.38),
        _features(),
        _benchmark(),
        now=NOW,
    )
    assert decision.selected_side is ContractSide.NO
    assert decision.action is DecisionAction.NO_TRADE
    assert decision.trade_direction is not Direction.UP


def test_buys_down_when_model_kalshi_align_and_down_has_edge():
    decision = _engine().decide(
        _market(yes_ask=0.50),
        _forecast(p_up=0.38),
        _features(),
        _benchmark(),
        now=NOW,
    )
    assert decision.selected_side is ContractSide.NO
    assert decision.action is DecisionAction.BUY_DOWN


def test_blocks_when_model_and_kalshi_disagree():
    decision = _engine().decide(
        _market(yes_ask=0.55),
        _forecast(p_up=0.38),
        _features(),
        _benchmark(),
        now=NOW,
    )
    assert decision.action is DecisionAction.NO_TRADE
    assert any(f.gate == "forecast_kalshi_alignment" for f in decision.gate_failures)


def test_edge_based_pick_still_available_when_enabled():
    decision = _engine(
        use_edge_based_side_pick=True,
        contrarian_fallback_enabled=False,
    ).decide(
        _market(yes_ask=0.21),
        _forecast(p_up=0.38),
        _features(),
        _benchmark(),
        now=NOW,
    )
    assert decision.selected_side is ContractSide.YES


def test_contrarian_fallback_only_when_perfect_mispricing():
    decision = _engine(
        contrarian_fallback_enabled=True,
        aligned_edge_premium=0.0,
        block_rally_contrarian_entries=False,
        forecast_alignment=ForecastAlignmentConfig(
            enabled=True,
            exceptional_edge_threshold=0.15,
            min_conflict_confidence=0.60,
            min_conflict_agreement=0.62,
            min_stability_confidence=0.55,
            min_stability_agreement=0.55,
        ),
    ).decide(
        _market(yes_ask=0.21),
        _forecast(p_up=0.38),
        _features(short_trend=0.0),
        _benchmark(),
        now=NOW,
    )
    assert decision.action is DecisionAction.BUY_UP
    assert "contrarian mispricing" in decision.reason
    assert decision.forecast_alignment is not None
    assert decision.forecast_alignment["entry_path"] == "contrarian"
