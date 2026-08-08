from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from kalshi_bot.config import AppConfig, ExecutionConfig, RiskConfig
from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    DecisionResult,
    Direction,
    ExecutionEstimate,
    MarketSnapshot,
)
from kalshi_bot.execution.engine import ExecutionEngine
from kalshi_bot.execution.position_manager import (
    DuplicateIntentError,
    PositionConflictError,
    PositionManager,
    PositionManagerConfig,
)
from kalshi_bot.execution.risk import RiskManager
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.models.probability import Confidence, EdgeSignal, Side

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def edge_signal(edge_pp: float) -> EdgeSignal:
    return EdgeSignal(
        ticker="KXBTC15M-TEST",
        series="KXBTC15M",
        side=Side.YES,
        kalshi_prob=0.52,
        options_prob=0.52 + edge_pp / 100,
        edge_pp=edge_pp,
        confidence=Confidence.HIGH,
        spread_cents=2,
        book_usd=100,
        strike=65000,
        spot=65010,
        iv=0.6,
        t_years=0.00001,
        reason="test",
    )


def market() -> MarketSnapshot:
    book = parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4800", "10"]],
                "no_dollars": [["0.4800", "10"]],
            }
        },
        timestamp=NOW,
    )
    return MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="CF Benchmarks BRTI",
        strike=65000,
        expiration=NOW + timedelta(minutes=5),
        open_time=NOW - timedelta(minutes=10),
        reference="BRTI",
        orderbook=book,
    )


def buy_decision(edge: float = 0.26) -> DecisionResult:
    execution = ExecutionEstimate(
        side=ContractSide.YES,
        quantity=1,
        filled_quantity=1,
        average_price=0.52,
        fee_per_contract=0,
        slippage_per_contract=0,
        total_cost=0.52,
        executable_cost=0.52,
        levels_consumed=1,
    )
    return DecisionResult(
        action=DecisionAction.BUY_UP,
        reason="valid edge",
        gate_failures=(),
        current_direction=Direction.UP,
        predicted_direction=Direction.UP,
        trade_direction=Direction.UP,
        selected_side=ContractSide.YES,
        predicted_probability=0.52 + edge,
        executable_cost=0.52,
        edge=edge,
        quantity=1,
        execution=execution,
    )


def test_legacy_path_cannot_bypass_hard_edge():
    cfg = AppConfig(
        execution=ExecutionConfig(
            max_position_usd=100,
            only_tiers=["HIGH", "MEDIUM", "LOW"],
        ),
        risk=RiskConfig(max_position_size=100, max_contract_exposure=100),
    )
    assert RiskManager(cfg).size_mispricing(edge_signal(19.999)) == 0
    assert RiskManager(cfg).size_mispricing(edge_signal(26)) > 0


def test_position_manager_requires_exit_before_flip_and_rejects_duplicates():
    manager = PositionManager(
        PositionManagerConfig(max_flips_per_contract=1, max_trades_per_contract=2)
    )
    manager.enter(
        intent_id="one",
        contract="T",
        side=ContractSide.YES,
        quantity=1,
        price=0.4,
        timestamp=NOW,
    )
    with pytest.raises(DuplicateIntentError):
        manager.enter(
            intent_id="one",
            contract="T",
            side=ContractSide.YES,
            quantity=1,
            price=0.4,
            timestamp=NOW,
        )
    with pytest.raises(PositionConflictError):
        manager.enter(
            intent_id="two",
            contract="T",
            side=ContractSide.NO,
            quantity=1,
            price=0.4,
            timestamp=NOW,
        )
    manager.exit(
        intent_id="exit",
        contract="T",
        price=0.6,
        timestamp=NOW + timedelta(seconds=1),
    )
    manager.enter(
        intent_id="flip",
        contract="T",
        side=ContractSide.NO,
        quantity=1,
        price=0.45,
        timestamp=NOW + timedelta(seconds=2),
    )
    assert manager.position("T").side is ContractSide.NO


def test_consecutive_losses_activate_risk_lock():
    cfg = AppConfig(risk=RiskConfig(max_consecutive_losses=2))
    risk = RiskManager(cfg)
    risk.register_pnl(-1)
    assert not risk.locked
    risk.register_pnl(-1)
    assert risk.locked
    assert "consecutive" in risk.state.halt_reason


def test_paper_execution_tracks_position_and_blocks_duplicate_position():
    cfg = AppConfig(
        execution=ExecutionConfig(dry_run=True, max_position_usd=100),
        risk=RiskConfig(
            max_position_size=100,
            max_contract_exposure=100,
            cooldown_seconds=0,
        ),
    )
    kalshi = MagicMock()
    kalshi.authenticated = False
    positions = PositionManager(mode="paper")
    risk = RiskManager(cfg, cooldown_sec=0, max_per_ticker_usd=100)
    engine = ExecutionEngine(kalshi, risk, cfg, positions=positions)
    report = engine.execute_decision(
        market(),
        buy_decision(),
        timestamp=NOW,
        intent_id="paper-entry",
    )
    assert report and report.ok and report.dry_run
    assert positions.position("KXBTC15M-TEST") is not None
    duplicate = engine.execute_decision(
        market(),
        buy_decision(),
        timestamp=NOW + timedelta(seconds=1),
        intent_id="paper-entry",
    )
    assert duplicate is not None
    assert not duplicate.ok


def test_live_execution_rejects_unofficial_proxy_even_with_valid_decision():
    cfg = AppConfig(
        execution=ExecutionConfig(dry_run=False, max_position_usd=100),
        risk=RiskConfig(
            max_position_size=100,
            max_contract_exposure=100,
            cooldown_seconds=0,
        ),
    )
    kalshi = MagicMock()
    kalshi.authenticated = True
    engine = ExecutionEngine(kalshi, RiskManager(cfg), cfg)
    proxy = BenchmarkQuote(
        price=65000,
        timestamp=NOW,
        source="Unofficial CME CF BRTI constituent proxy",
        primary=False,
        is_proxy=True,
        constituent_count=3,
        dispersion=0.0001,
    )
    report = engine.execute_decision(
        market(),
        buy_decision(),
        timestamp=NOW,
        benchmark=proxy,
    )
    assert report is not None
    assert not report.ok
    assert "official primary BRTI" in report.detail
    kalshi.create_order.assert_not_called()
