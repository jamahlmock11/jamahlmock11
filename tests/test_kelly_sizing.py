from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.config import AppConfig, ExecutionConfig, RiskConfig
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
from kalshi_bot.execution.risk import (
    RiskManager,
    quarter_kelly_bankroll_fraction,
    quarter_kelly_notional_usd,
)
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.strategies.decision import DecisionConfig, DecisionEngine

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def test_quarter_kelly_fraction_matches_formula():
  assert quarter_kelly_bankroll_fraction(0.20, kelly_fraction=0.25) == pytest.approx(
      0.20 / 0.80 * 0.25
  )
  assert quarter_kelly_notional_usd(0.20, 100.0, kelly_fraction=0.25) == pytest.approx(6.25)


def test_kelly_contracts_scale_with_edge():
    cfg = AppConfig(
        execution=ExecutionConfig(max_contracts_per_trade=25, min_trade_notional_usd=0),
        risk=RiskConfig(
            max_position_size=100,
            max_contract_exposure=100,
            kelly_enabled=True,
            kelly_fraction=0.25,
            kelly_bankroll_usd=100,
        ),
    )
    risk = RiskManager(cfg, max_per_ticker_usd=100)
    small = risk.kelly_contracts_for_entry(edge=0.20, executable_cost=0.50)
    large = risk.kelly_contracts_for_entry(edge=0.30, executable_cost=0.50)
    assert large > small > 0


def book(yes_ask: float = 0.52, level_depth: str = "100"):
    no_bid = 1.0 - yes_ask
    yes_bid = yes_ask - 0.02
    return parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_bid:.4f}", level_depth]],
                "no_dollars": [[f"{no_bid:.4f}", level_depth]],
            }
        },
        timestamp=NOW,
    )


def market(yes_ask: float = 0.52) -> MarketSnapshot:
    return MarketSnapshot(
        ticker="KXBTC15M-26AUG080815-00",
        status="active",
        rules="BRTI",
        strike=65_000,
        expiration=NOW + timedelta(minutes=10),
        open_time=NOW - timedelta(minutes=5),
        reference="BRTI",
        orderbook=book(yes_ask),
    )


def features() -> FeatureSnapshot:
    return FeatureSnapshot(
        timestamp=NOW,
        current_price=65_020,
        strike=65_000,
        seconds_remaining=600,
        changes={5: 0.0002, 10: 0.0003, 15: 0.0004, 30: 0.0005, 60: 0.0006, 120: 0.0007},
        velocities={5: 0.00004, 10: 0.00003, 15: 0.000026},
        acceleration=0.000001,
        short_trend=0.0003,
        medium_trend=0.0006,
        realized_vol=0.65,
        expected_remaining_move=80,
        z_distance_to_strike=0.25,
        mean_reversion_score=-0.1,
        orderbook_imbalance=0.1,
        cross_venue_agreement=0.9,
        cross_venue_dispersion=0.0002,
        data_completeness=1.0,
        trajectory=TrajectoryState.ACCELERATING_UP,
        sample_count=301,
        oldest_sample_age=300,
    )


def forecast(p_up: float) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        p_up=p_up,
        p_down=1 - p_up,
        confidence=0.9,
        signal_agreement=0.8,
        component_probabilities={"terminal_distribution": p_up},
        regime=Regime.TREND_UP if p_up >= 0.5 else Regime.TREND_DOWN,
        raw_p_up=p_up,
    )


def benchmark() -> BenchmarkQuote:
    return BenchmarkQuote(
        price=65_020,
        timestamp=NOW,
        source="CME CF Bitcoin Real Time Index (BRTI)",
    )


def test_decision_pipeline_applies_kelly_quantity():
    cfg = AppConfig(
        execution=ExecutionConfig(max_contracts_per_trade=25, min_trade_notional_usd=0),
        risk=RiskConfig(
            max_position_size=100,
            max_contract_exposure=100,
            kelly_enabled=True,
            kelly_fraction=0.25,
            kelly_bankroll_usd=100,
        ),
    )
    risk = RiskManager(cfg, max_per_ticker_usd=100)
    result = DecisionEngine(DecisionConfig(minimum_agreement=0.48)).decide(
        market(),
        forecast(0.78),
        features(),
        benchmark(),
        now=NOW,
        risk_manager=risk,
    )
    assert result.action is DecisionAction.BUY_UP
    assert result.quantity > 1


def test_decision_blocks_when_exit_liquidity_insufficient():
    yes_ask = 0.52
    yes_bid = yes_ask - 0.02
    no_bid = 1.0 - yes_ask
    thin_book = parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_bid:.4f}", "1"]],
                "no_dollars": [[f"{no_bid:.4f}", "100"]],
            }
        },
        timestamp=NOW,
    )
    cfg = AppConfig(
        execution=ExecutionConfig(max_contracts_per_trade=25, min_trade_notional_usd=0),
        risk=RiskConfig(
            max_position_size=1000,
            max_contract_exposure=1000,
            kelly_enabled=True,
            kelly_fraction=0.25,
            kelly_bankroll_usd=1000,
        ),
    )
    risk = RiskManager(cfg, max_per_ticker_usd=1000)
    result = DecisionEngine(DecisionConfig(minimum_agreement=0.48)).decide(
        replace(market(), orderbook=thin_book),
        forecast(0.78),
        features(),
        benchmark(),
        now=NOW,
        risk_manager=risk,
    )
    assert result.action is DecisionAction.NO_TRADE
    assert any(failure.gate == "exit_liquidity" for failure in result.gate_failures)
