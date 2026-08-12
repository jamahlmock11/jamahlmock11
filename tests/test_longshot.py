"""Tests for longshot-only entry filters and cent-based exits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kalshi_bot.config import LongshotConfig
from kalshi_bot.domain import ContractSide, MarketPosition
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.strategies.longshot import (
    LongshotExitConfig,
    evaluate_longshot_exit,
    filter_crowd_follow_executions,
    filter_longshot_executions,
    longshot_price_gate,
)
from kalshi_bot.market.orderbook import estimate_buy_execution

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
CFG = LongshotConfig(enabled=True)


def book(yes_ask: float):
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


def market(yes_ask: float):
    from kalshi_bot.domain import MarketSnapshot

    return MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="BRTI",
        strike=65000,
        expiration=NOW + timedelta(minutes=10),
        open_time=NOW - timedelta(minutes=5),
        reference="BRTI",
        orderbook=book(yes_ask),
    )


def test_filter_longshot_executions_keeps_cheap_side_only():
    book_obj = book(0.35)
    executions = {
        ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
        ContractSide.NO: estimate_buy_execution(book_obj, ContractSide.NO, 1),
    }
    filtered = filter_longshot_executions(executions, max_entry_price=0.45)
    assert ContractSide.YES in filtered
    assert ContractSide.NO not in filtered


def test_filter_crowd_follow_executions_keeps_band_only():
    book_obj = book(0.90)
    executions = {
        ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
    }
    filtered = filter_crowd_follow_executions(
        executions,
        min_entry_price=0.87,
        max_entry_price=0.93,
    )
    assert ContractSide.YES in filtered

    expensive = book(0.96)
    expensive_exec = {
        ContractSide.YES: estimate_buy_execution(expensive, ContractSide.YES, 1),
    }
    filtered_expensive = filter_crowd_follow_executions(
        expensive_exec,
        min_entry_price=0.87,
        max_entry_price=0.93,
    )
    assert not filtered_expensive


def test_longshot_price_gate_blocks_favorites():
    book_obj = book(0.50)
    executions = filter_longshot_executions(
        {
            ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
            ContractSide.NO: estimate_buy_execution(book_obj, ContractSide.NO, 1),
        },
        max_entry_price=0.45,
    )
    failure = longshot_price_gate(executions, cfg=CFG)
    assert failure is not None
    assert failure.gate == "longshot_price"


def test_take_profit_cents_triggers_exit():
    position = MarketPosition(
        side=ContractSide.YES,
        quantity=1,
        average_price=0.35,
        opened_at=NOW - timedelta(seconds=30),
    )
    signal = evaluate_longshot_exit(
        market=market(0.47),
        position=position,
        quantity=1,
        cfg=LongshotExitConfig(take_profit_cents=0.10),
        now=NOW,
    )
    assert signal is not None
    assert signal.trigger == "take_profit_cents"


def test_stop_loss_cents_triggers_exit():
    position = MarketPosition(
        side=ContractSide.YES,
        quantity=1,
        average_price=0.35,
        opened_at=NOW - timedelta(seconds=30),
    )
    signal = evaluate_longshot_exit(
        market=market(0.25),
        position=position,
        quantity=1,
        cfg=LongshotExitConfig(stop_loss_cents=0.08),
        now=NOW,
    )
    assert signal is not None
    assert signal.trigger == "stop_loss_cents"


def test_reversal_check_triggers_early_exit():
    position = MarketPosition(
        side=ContractSide.YES,
        quantity=1,
        average_price=0.35,
        opened_at=NOW - timedelta(seconds=60),
    )
    signal = evaluate_longshot_exit(
        market=market(0.28),
        position=position,
        quantity=1,
        cfg=LongshotExitConfig(
            stop_loss_cents=0.20,
            stop_loss_pct=1.0,
            reversal_cents=0.05,
            reversal_window_seconds=120,
        ),
        now=NOW,
    )
    assert signal is not None
    assert signal.trigger == "reversal_check"
