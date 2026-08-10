"""Tests for late extreme-poll crowd following."""

from __future__ import annotations

from datetime import datetime, timezone

from kalshi_bot.config import LongshotConfig
from kalshi_bot.domain import ContractSide, ProbabilityEstimate, Regime
from kalshi_bot.market.orderbook import estimate_buy_execution, parse_orderbook_fp
from kalshi_bot.market.poll_alignment import market_poll_snapshot
from kalshi_bot.strategies.longshot import resolve_longshot_entries

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


def forecast(p_up: float):
    return ProbabilityEstimate(
        p_up=p_up,
        p_down=1 - p_up,
        confidence=0.65,
        signal_agreement=0.70,
        component_probabilities={"terminal": p_up},
        regime=Regime.TREND_UP,
        raw_p_up=p_up,
    )


def test_blocks_contrarian_yes_when_no_poll_is_99_late():
    book_obj = book(0.02)
    executions = {
        ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
        ContractSide.NO: estimate_buy_execution(book_obj, ContractSide.NO, 1),
    }
    ctx = resolve_longshot_entries(
        executions,
        poll=market_poll_snapshot(book_obj),
        forecast=forecast(0.24),
        seconds_remaining=120,
        cfg=CFG,
    )
    assert any(f.gate == "favorite_poll_contrarian" for f in ctx.failures)
    assert ContractSide.YES not in ctx.executions
    assert ctx.forced_side is ContractSide.NO


def test_blocks_contrarian_at_87_percent_favorite():
    book_obj = book(0.15)
    executions = {
        ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
        ContractSide.NO: estimate_buy_execution(book_obj, ContractSide.NO, 1),
    }
    ctx = resolve_longshot_entries(
        executions,
        poll=market_poll_snapshot(book_obj),
        forecast=forecast(0.20),
        seconds_remaining=600,
        cfg=CFG,
    )
    assert any(f.gate == "favorite_poll_contrarian" for f in ctx.failures)
    assert ctx.forced_side is ContractSide.NO
    assert ContractSide.YES not in ctx.executions


def test_allows_expensive_no_favorite_when_poll_is_99_late():
    book_obj = book(0.03)
    executions = {
        ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
        ContractSide.NO: estimate_buy_execution(book_obj, ContractSide.NO, 1),
    }
    ctx = resolve_longshot_entries(
        executions,
        poll=market_poll_snapshot(book_obj),
        forecast=forecast(0.30),
        seconds_remaining=120,
        cfg=CFG,
    )
    assert ContractSide.NO in ctx.executions
    assert ctx.executions[ContractSide.NO].executable_cost > 0.45
    assert ctx.min_edge_override == -1.0
