"""Tests for late crowd strike-distance and hold-direction gates."""

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
from kalshi_bot.strategies.crowd_strike_hold import (
    crowd_strike_hold_gate,
    evaluate_crowd_strike_hold,
)
from kalshi_bot.strategies.longshot import resolve_longshot_entries

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
POLL_CFG = PollConfig()


def features(
    *,
    spot: float,
    strike: float,
    seconds_remaining: float,
    z_distance: float,
    p_up: float = 0.46,
):
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


def forecast(p_up: float):
    return ProbabilityEstimate(
        p_up=p_up,
        p_down=1 - p_up,
        confidence=0.33,
        signal_agreement=0.50,
        component_probabilities={"terminal": p_up},
        regime=Regime.TREND_UP,
        raw_p_up=p_up,
    )


def test_strike_hold_supports_no_when_spot_below_strike_and_model_54_down():
    cfg = LongshotConfig()
    assessment = evaluate_crowd_strike_hold(
        features(spot=63_900.0, strike=63_915.0, seconds_remaining=400, z_distance=-0.4),
        forecast(0.46),
        crowd_side=ContractSide.NO,
        cfg=cfg,
    )
    assert assessment.hold_side is ContractSide.NO
    assert assessment.hold_probability >= 0.50
    assert assessment.supports_crowd
    assert "hold DOWN" in assessment.summary
    assert crowd_strike_hold_gate(assessment, cfg=cfg) is None


def test_strike_hold_blocks_no_when_spot_far_above_strike():
    cfg = LongshotConfig(late_crowd_max_z_against=1.0)
    assessment = evaluate_crowd_strike_hold(
        features(spot=64_200.0, strike=63_500.0, seconds_remaining=400, z_distance=2.5),
        forecast(0.46),
        crowd_side=ContractSide.NO,
        cfg=cfg,
    )
    assert not assessment.supports_crowd
    failure = crowd_strike_hold_gate(assessment, cfg=cfg)
    assert failure is not None
    assert failure.gate == "crowd_strike_hold"


def test_late_crowd_entry_includes_strike_hold_context():
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
        favorite_only=True,
        late_crowd_follow_seconds=540,
        late_crowd_poll_threshold=0.84,
        late_crowd_min_model_prob=0.50,
        late_crowd_favorite_max_price=0.86,
    )
    ctx = resolve_longshot_entries(
        executions,
        poll=market_poll_snapshot(book_obj),
        forecast=forecast(0.46),
        seconds_remaining=480,
        cfg=cfg,
        poll_cfg=POLL_CFG,
        features=features(
            spot=63_900.0,
            strike=63_915.0,
            seconds_remaining=480,
            z_distance=-0.4,
        ),
    )
    assert ctx.strike_hold is not None
    assert ctx.strike_hold.supports_crowd
    assert ContractSide.NO in ctx.executions
    assert "hold DOWN" in ctx.strike_hold.summary
