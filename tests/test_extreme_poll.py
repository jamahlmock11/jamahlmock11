"""Tests for late extreme-poll crowd following."""

from __future__ import annotations

from datetime import datetime, timezone

from kalshi_bot.config import LongshotConfig
from kalshi_bot.domain import ContractSide, ProbabilityEstimate, Regime
from kalshi_bot.market.orderbook import estimate_buy_execution, parse_orderbook_fp
from kalshi_bot.market.poll_alignment import PollConfig, market_poll_snapshot
from kalshi_bot.strategies.longshot import resolve_longshot_entries

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
CFG = LongshotConfig(enabled=True, extreme_favorite_max_price=0.85)
POLL_CFG = PollConfig()


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


def forecast(
    p_up: float,
    *,
    confidence: float = 0.65,
    agreement: float = 0.70,
):
    return ProbabilityEstimate(
        p_up=p_up,
        p_down=1 - p_up,
        confidence=confidence,
        signal_agreement=agreement,
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
        poll_cfg=POLL_CFG,
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
        poll_cfg=POLL_CFG,
    )
    assert any(f.gate == "favorite_poll_contrarian" for f in ctx.failures)
    assert ctx.forced_side is ContractSide.NO
    assert ContractSide.YES not in ctx.executions


def test_favorite_only_blocks_entries_when_poll_below_threshold():
    book_obj = book(0.50)
    executions = {
        ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
        ContractSide.NO: estimate_buy_execution(book_obj, ContractSide.NO, 1),
    }
    ctx = resolve_longshot_entries(
        executions,
        poll=market_poll_snapshot(book_obj),
        forecast=forecast(0.55),
        seconds_remaining=600,
        cfg=LongshotConfig(enabled=True, favorite_only=True),
        poll_cfg=POLL_CFG,
    )
    assert any(f.gate == "favorite_only" for f in ctx.failures)
    assert not ctx.executions


def test_crowd_follow_requires_model_direction_match():
    book_obj = book(0.03)
    executions = {
        ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
        ContractSide.NO: estimate_buy_execution(book_obj, ContractSide.NO, 1),
    }
    ctx = resolve_longshot_entries(
        executions,
        poll=market_poll_snapshot(book_obj),
        forecast=forecast(0.80, confidence=0.50, agreement=0.50),
        seconds_remaining=120,
        cfg=CFG,
        poll_cfg=POLL_CFG,
    )
    assert any(f.gate == "crowd_model_direction" for f in ctx.failures)
    assert not ctx.executions


def test_crowd_follow_selects_favorite_with_aligned_model():
    book_obj = parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.845", "1000"]],
                "no_dollars": [["0.145", "1000"]],
            }
        },
        timestamp=NOW,
    )
    executions = {
        ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
        ContractSide.NO: estimate_buy_execution(book_obj, ContractSide.NO, 1),
    }
    cfg = LongshotConfig(enabled=True, extreme_favorite_max_price=0.86)
    ctx = resolve_longshot_entries(
        executions,
        poll=market_poll_snapshot(book_obj),
        forecast=forecast(0.88),
        seconds_remaining=600,
        cfg=cfg,
        poll_cfg=POLL_CFG,
    )
    assert ctx.forced_side is ContractSide.YES
    assert ContractSide.YES in ctx.executions
    assert ctx.min_edge_override == cfg.min_edge


def test_blocks_expensive_favorite_above_85_cent_cap():
    book_obj = book(0.03)
    executions = {
        ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
        ContractSide.NO: estimate_buy_execution(book_obj, ContractSide.NO, 1),
    }
    ctx = resolve_longshot_entries(
        executions,
        poll=market_poll_snapshot(book_obj),
        forecast=forecast(0.20),
        seconds_remaining=120,
        cfg=CFG,
        poll_cfg=POLL_CFG,
    )
    assert not ctx.executions
    assert any(f.gate == "longshot_price" for f in ctx.failures)


def test_reversal_allows_contrarian_with_full_counter_evidence():
    book_obj = book(0.02)
    executions = {
        ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
        ContractSide.NO: estimate_buy_execution(book_obj, ContractSide.NO, 1),
    }
    ctx = resolve_longshot_entries(
        executions,
        poll=market_poll_snapshot(book_obj),
        forecast=forecast(0.78, confidence=0.70, agreement=0.70),
        seconds_remaining=120,
        cfg=CFG,
        poll_cfg=POLL_CFG,
    )
    assert ctx.forced_side is ContractSide.YES
    assert ContractSide.YES in ctx.executions
    assert not any(f.gate == "favorite_poll_contrarian" for f in ctx.failures)
