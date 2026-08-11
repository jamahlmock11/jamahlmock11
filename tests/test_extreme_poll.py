"""Tests for late extreme-poll crowd following."""

from __future__ import annotations

from datetime import datetime, timezone

from kalshi_bot.config import LongshotConfig
from kalshi_bot.domain import (
    ContractSide,
    FeatureSnapshot,
    ProbabilityEstimate,
    Regime,
    TrajectoryState,
)
from kalshi_bot.market.orderbook import estimate_buy_execution, parse_orderbook_fp
from kalshi_bot.market.poll_alignment import PollConfig, market_poll_snapshot
from kalshi_bot.strategies.longshot import resolve_longshot_entries

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
CFG = LongshotConfig(enabled=True, follow_extreme_poll=True, extreme_favorite_max_price=0.85)
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


def features(spot: float, strike: float, *, seconds_remaining: float, z_distance: float):
    return FeatureSnapshot(
        timestamp=NOW,
        current_price=spot,
        strike=strike,
        seconds_remaining=seconds_remaining,
        changes={},
        velocities={},
        acceleration=0.0,
        short_trend=0.0,
        medium_trend=0.0,
        realized_vol=0.5,
        expected_remaining_move=abs(spot - strike) / max(abs(z_distance), 0.1),
        z_distance_to_strike=z_distance,
        mean_reversion_score=0.0,
        orderbook_imbalance=0.0,
        cross_venue_agreement=1.0,
        cross_venue_dispersion=0.0,
        data_completeness=1.0,
        trajectory=TrajectoryState.FLAT,
        sample_count=100,
        oldest_sample_age=0.0,
    )


def test_follows_no_favorite_without_model_support():
    book_obj = book(0.02)
    executions = {
        ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
        ContractSide.NO: estimate_buy_execution(book_obj, ContractSide.NO, 1),
    }
    ctx = resolve_longshot_entries(
        executions,
        poll=market_poll_snapshot(book_obj),
        forecast=forecast(0.80),
        seconds_remaining=120,
        cfg=CFG,
        poll_cfg=POLL_CFG,
    )
    assert ctx.forced_side is ContractSide.NO
    assert ContractSide.YES not in ctx.executions
    assert not any(f.gate == "crowd_model_direction" for f in ctx.failures)
    assert not any(f.gate == "favorite_poll_model" for f in ctx.failures)


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


def test_crowd_follow_waives_edge_and_ignores_model_direction():
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
    cfg = LongshotConfig(enabled=True, follow_extreme_poll=True, extreme_favorite_max_price=0.86)
    ctx = resolve_longshot_entries(
        executions,
        poll=market_poll_snapshot(book_obj),
        forecast=forecast(0.20),
        seconds_remaining=600,
        cfg=cfg,
        poll_cfg=POLL_CFG,
    )
    assert ctx.forced_side is ContractSide.YES
    assert ContractSide.YES in ctx.executions
    assert ctx.min_edge_override == -1.0


def test_blocks_expensive_favorite_above_price_cap():
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


def test_late_crowd_follow_allows_84_percent_no_without_model():
    book_obj = parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.14", "1000"]],
                "no_dollars": [["0.84", "1000"]],
            }
        },
        timestamp=NOW,
    )
    executions = {
        ContractSide.YES: estimate_buy_execution(book_obj, ContractSide.YES, 1),
        ContractSide.NO: estimate_buy_execution(book_obj, ContractSide.NO, 1),
    }
    cfg = LongshotConfig(
        enabled=True,
        follow_extreme_poll=True,
        favorite_only=True,
        extreme_favorite_max_price=0.85,
        late_crowd_follow_seconds=540,
        late_crowd_poll_threshold=0.84,
        late_crowd_favorite_max_price=0.86,
    )
    ctx = resolve_longshot_entries(
        executions,
        poll=market_poll_snapshot(book_obj),
        forecast=forecast(0.80),
        seconds_remaining=480,
        cfg=cfg,
        poll_cfg=POLL_CFG,
        features=features(63_900.0, 63_915.0, seconds_remaining=480, z_distance=-0.4),
    )
    assert ctx.forced_side is ContractSide.NO
    assert ContractSide.NO in ctx.executions
    assert ctx.min_edge_override == -1.0
    assert ctx.strike_hold is not None
