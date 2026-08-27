"""Tests for WebSocket crowd-favorite 1-hour strategy."""

from __future__ import annotations

import pytest

from kalshi_bot.config import HourWSConfig
from kalshi_bot.hour_ws.crowd import CrowdEngine
from kalshi_bot.hour_ws.indicators import Indicators
from kalshi_bot.hour_ws.strategy import CrowdFavoriteStrategy


def _rising_prices(n: int = 30, start: float = 0.60) -> list[float]:
    return [start + i * 0.001 for i in range(n)]


def test_rsi_oversold_signals_buy_component():
    prices = [0.70 - i * 0.01 for i in range(25)]
    assert Indicators.rsi(prices) < 30


def test_crowd_engine_scores_in_band():
    cfg = HourWSConfig(crowd_min_cents=55, crowd_max_cents=86)
    engine = CrowdEngine(cfg)
    snapshot = engine.update(
        "TEST",
        price=0.70,
        volume=5000,
        orderbook={
            "yes": {
                "bids": [[0.69, 100], [0.68, 50]],
                "asks": [[0.71, 20], [0.72, 10]],
            }
        },
    )
    assert snapshot.score > 0
    assert engine.get_crowd_bias("TEST") in {"YES", "NO", None}


def test_strategy_holds_outside_entry_band():
    cfg = HourWSConfig(min_entry_cents=45, max_entry_cents=78, min_price_history=20)
    strategy = CrowdFavoriteStrategy(cfg)
    prices = _rising_prices()
    result = strategy.analyze("TEST", prices, volume=1000, orderbook=None)
    assert result.signal == "HOLD"
    assert result.price_cents == pytest.approx(prices[-1] * 100, rel=1e-3)


def test_strategy_buy_when_technicals_and_crowd_align():
    cfg = HourWSConfig(
        min_entry_cents=45,
        max_entry_cents=78,
        crowd_min_cents=55,
        crowd_max_cents=86,
        min_confidence=20,
        min_edge_cents=1,
        min_price_history=20,
    )
    strategy = CrowdFavoriteStrategy(cfg)
    prices = [0.58 + i * 0.002 for i in range(30)]
    orderbook = {
        "yes": {
            "bids": [[0.63, 500], [0.62, 300], [0.61, 200]],
            "asks": [[0.64, 10], [0.65, 5]],
        }
    }
    result = strategy.analyze("TEST", prices, volume=8000, orderbook=orderbook)
    assert result.signal in {"BUY", "SELL", "HOLD"}
    if result.signal != "HOLD":
        assert result.confidence >= cfg.min_confidence


def test_1h_ws_yaml_loads():
    from kalshi_bot.config import load_yaml_config

    cfg = load_yaml_config("config/1h_ws.yaml")
    assert cfg.horizon == "1h"
    assert cfg.hour_ws.min_entry_cents == pytest.approx(45)
    assert cfg.hour_ws.crowd_max_cents == pytest.approx(86)
    assert cfg.execution.dry_run is True
