"""Tests for shared 15m decision rules on the 1-hour bot."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.config import load_yaml_config
from kalshi_bot.domain import BenchmarkQuote, DecisionAction, FeatureSnapshot, TrajectoryState
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.strategies.decision import DecisionConfig, DecisionEngine

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _book(yes_ask: float = 0.82):
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


def test_poll_favorite_gate_blocks_low_probability_market():
    from tests.test_hour_bot import benchmark, forecast, hour_market

    cfg = DecisionConfig(
        minimum_dominant_poll=0.80,
        require_dominant_poll_side=True,
        maximum_seconds_remaining=2400,
        minimum_seconds_remaining=60,
    )
    engine = DecisionEngine(cfg)
    market = hour_market(0.55, minutes_remaining=30)
    decision = engine.decide(
        market,
        forecast(p_up=0.62),
        FeatureSnapshot(
            timestamp=NOW,
            current_price=65_020,
            strike=65_000,
            seconds_remaining=1800,
            changes={60: 0.001},
            velocities={60: 0.0001},
            acceleration=0.0,
            short_trend=0.001,
            medium_trend=0.001,
            realized_vol=0.5,
            expected_remaining_move=100,
            z_distance_to_strike=0.1,
            mean_reversion_score=0.0,
            orderbook_imbalance=0.0,
            cross_venue_agreement=0.9,
            cross_venue_dispersion=0.0001,
            data_completeness=0.9,
            trajectory=TrajectoryState.ACCELERATING_UP,
            sample_count=100,
            oldest_sample_age=3600,
        ),
        benchmark(),
        now=NOW,
    )
    assert decision.action is DecisionAction.NO_TRADE
    assert any(f.gate == "poll_favorite" for f in decision.gate_failures)


def test_decision_config_from_app_uses_hour_entry_window():
    from kalshi_bot.strategies.decision import decision_config_from_app

    cfg = load_yaml_config("config/1h.yaml")
    decision_cfg = decision_config_from_app(
        cfg,
        maximum_seconds_remaining=cfg.hour.max_entry_seconds_remaining,
    )
    assert decision_cfg.maximum_seconds_remaining == pytest.approx(3300)
    assert decision_cfg.minimum_dominant_poll is None
    assert decision_cfg.require_dominant_poll_side is False
    assert decision_cfg.longshot.enabled is False
    assert cfg.terminal_probability.enabled is True
    assert cfg.strategy.min_edge == pytest.approx(0.10)
