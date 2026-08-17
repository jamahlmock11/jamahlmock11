"""Tests for multi-strike hourly discovery and selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kalshi_bot.config import HourStrategyConfig
from kalshi_bot.domain import ContractSide, DecisionAction, DecisionResult, Direction, MarketSnapshot
from kalshi_bot.hour.discovery import HourDiscoveryConfig, discover_all_hour_markets
from kalshi_bot.hour.mispricing import MispricingAssessment
from kalshi_bot.hour.strike_selection import (
    rank_terminal_candidate,
    select_best_strike_candidate,
    StrikeCandidateResult,
    StrikeRankConfig,
)
from kalshi_bot.hour.terminal_probability import TerminalForecast
from kalshi_bot.market.orderbook import parse_orderbook_fp
from tests.test_hour_discovery import FakeMarket, NOW, _book


def _market(strike: float, yes_ask: float) -> MarketSnapshot:
    book = parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_ask - 0.02:.4f}", "100"]],
                "no_dollars": [[f"{1.0 - yes_ask:.4f}", "100"]],
            }
        },
        timestamp=NOW,
    )
    return MarketSnapshot(
        ticker=f"KXBTCD-T{int(strike)}",
        status="active",
        rules="CF Benchmarks Bitcoin Real Time Index (BRTI)",
        strike=strike,
        expiration=NOW + timedelta(minutes=30),
        open_time=NOW - timedelta(minutes=30),
        reference="CME CF Bitcoin Real Time Index (BRTI)",
        orderbook=book,
    )


def _decision(action: DecisionAction, edge: float | None = None) -> DecisionResult:
    return DecisionResult(
        action=action,
        reason="test",
        gate_failures=(),
        current_direction=Direction.FLAT,
        predicted_direction=Direction.UP,
        trade_direction=Direction.FLAT,
        edge=edge,
    )


def test_discover_all_hour_markets_returns_same_expiration_strikes():
    hour_cfg = HourStrategyConfig(series_ticker="KXBTCD", contract_duration_seconds=3600)
    config = HourDiscoveryConfig(hour=hour_cfg)
    exp = NOW + timedelta(minutes=30)
    markets = [
        FakeMarket(ticker="KXBTCD-A", floor_strike=64000, close_time=exp),
        FakeMarket(ticker="KXBTCD-B", floor_strike=64500, close_time=exp),
        FakeMarket(ticker="KXBTCD-C", floor_strike=70000, close_time=exp + timedelta(hours=1)),
    ]
    books = {m.ticker: _book() for m in markets}
    batch = discover_all_hour_markets(
        markets,
        orderbooks=books,
        now=NOW,
        config=config,
        reference_price=64300,
        strike_count=5,
    )
    assert len(batch.markets) == 2
    assert {m.strike for m in batch.markets} == {64000.0, 64500.0}


def test_select_best_strike_prefers_strong_evidence_outer_strike():
    rank_cfg = StrikeRankConfig(strong_evidence_min_probability=0.78)
    terminal_strong = TerminalForecast(
        p_yes=0.12,
        p_no=0.88,
        raw_p_yes=0.12,
        calibrated_p_yes=0.12,
        calibrated_p_no=0.88,
        expected_terminal_brti=64000,
        terminal_volatility=0.01,
        distance_from_strike=500,
        normalized_strike_distance=1.0,
        confidence=0.70,
        signal_agreement=0.60,
        component_probabilities={},
        strike=65500,
        current_brti=65000,
        seconds_remaining=1800,
        settlement_reference="BRTI",
    )
    terminal_weak = TerminalForecast(
        p_yes=0.55,
        p_no=0.45,
        raw_p_yes=0.55,
        calibrated_p_yes=0.55,
        calibrated_p_no=0.45,
        expected_terminal_brti=65020,
        terminal_volatility=0.01,
        distance_from_strike=20,
        normalized_strike_distance=0.1,
        confidence=0.55,
        signal_agreement=0.50,
        component_probabilities={},
        strike=64299,
        current_brti=65020,
        seconds_remaining=1800,
        settlement_reference="BRTI",
    )
    mispricing_strong = MispricingAssessment(
        yes=None,
        no=None,
        best_side=ContractSide.NO,
        best_net_edge=0.14,
        required_edge=0.10,
        yes_net_edge=-0.20,
        no_net_edge=0.14,
    )
    mispricing_weak = MispricingAssessment(
        yes=None,
        no=None,
        best_side=ContractSide.YES,
        best_net_edge=0.05,
        required_edge=0.10,
        yes_net_edge=0.05,
        no_net_edge=-0.08,
    )
    outer = StrikeCandidateResult(
        market=_market(65500, 0.72),
        terminal=terminal_strong,
        decision=_decision(DecisionAction.NO_TRADE, 0.14),
        mispricing=mispricing_strong,
        stability_swing=0.0,
        rank_key=rank_terminal_candidate(
            market=_market(65500, 0.72),
            decision=_decision(DecisionAction.NO_TRADE, 0.14),
            mispricing=mispricing_strong,
            terminal=terminal_strong,
            has_position=False,
            rank_cfg=rank_cfg,
        ),
        summary="outer",
    )
    inner = StrikeCandidateResult(
        market=_market(64299, 0.55),
        terminal=terminal_weak,
        decision=_decision(DecisionAction.NO_TRADE, 0.05),
        mispricing=mispricing_weak,
        stability_swing=0.0,
        rank_key=rank_terminal_candidate(
            market=_market(64299, 0.55),
            decision=_decision(DecisionAction.NO_TRADE, 0.05),
            mispricing=mispricing_weak,
            terminal=terminal_weak,
            has_position=False,
            rank_cfg=rank_cfg,
        ),
        summary="inner",
    )
    best = select_best_strike_candidate([inner, outer])
    assert best is outer


def test_select_best_strike_prefers_tradeable_favorite_candidate():
    mispricing_good = MispricingAssessment(
        yes=None,
        no=None,
        best_side=None,
        best_net_edge=0.20,
        required_edge=0.12,
        yes_net_edge=-0.04,
        no_net_edge=0.20,
    )
    mispricing_far = MispricingAssessment(
        yes=None,
        no=None,
        best_side=None,
        best_net_edge=-0.04,
        required_edge=0.16,
        yes_net_edge=-0.04,
        no_net_edge=-0.10,
    )
    far = StrikeCandidateResult(
        market=_market(64799, 0.75),
        terminal=None,
        decision=_decision(DecisionAction.NO_TRADE, -0.04),
        mispricing=mispricing_far,
        stability_swing=0.0,
        rank_key=rank_terminal_candidate(
            market=_market(64799, 0.75),
            decision=_decision(DecisionAction.NO_TRADE, -0.04),
            mispricing=mispricing_far,
            has_position=False,
        ),
        summary="far",
    )
    close = StrikeCandidateResult(
        market=_market(64299, 0.68),
        terminal=None,
        decision=_decision(DecisionAction.BUY_DOWN, 0.20),
        mispricing=mispricing_good,
        stability_swing=0.0,
        rank_key=rank_terminal_candidate(
            market=_market(64299, 0.68),
            decision=_decision(DecisionAction.BUY_DOWN, 0.20),
            mispricing=mispricing_good,
            has_position=False,
        ),
        summary="close",
    )
    best = select_best_strike_candidate([far, close])
    assert best is close
    assert best.decision.action is DecisionAction.BUY_DOWN
