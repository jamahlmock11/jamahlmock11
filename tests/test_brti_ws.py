"""Tests for Kalshi cfbenchmarks_value BRTI websocket parsing."""

from __future__ import annotations

import json

import pytest

from kalshi_bot.brti_ws.feed import BRTIFeed, parse_cfbenchmarks_value_message
from kalshi_bot.config import load_yaml_config


def test_parse_cfbenchmarks_value_nested_json():
    upstream = {"type": "value", "id": "BRTI", "time": 1_700_000_000_000, "value": "65020.5"}
    msg = {
        "type": "cfbenchmarks_value",
        "msg": {
            "index_id": "BRTI",
            "received_at": 1_700_000_000_500,
            "data": json.dumps(upstream),
        },
    }
    tick = parse_cfbenchmarks_value_message(msg)
    assert tick is not None
    assert tick.price == pytest.approx(65020.5)
    assert tick.ts_ms == 1_700_000_000_000


def test_parse_cfbenchmarks_value_flat_body():
    msg = {
        "type": "cfbenchmarks_value",
        "msg": {
            "index_id": "BRTI",
            "time": 1_700_000_001_000,
            "value": "65100.0",
        },
    }
    tick = parse_cfbenchmarks_value_message(msg)
    assert tick is not None
    assert tick.price == pytest.approx(65100.0)
    assert tick.ts_ms == 1_700_000_001_000


def test_parse_cfbenchmarks_value_ignores_other_indices():
    msg = {
        "type": "cfbenchmarks_value",
        "msg": {"index_id": "ETHUSD_RTI", "value": "3200.0", "time": 1},
    }
    assert parse_cfbenchmarks_value_message(msg) is None


@pytest.mark.asyncio
async def test_brti_feed_sma_and_lag():
    from kalshi_bot.config import BrtiWSConfig
    from kalshi_bot.venues.kalshi import KalshiClient

    cfg = BrtiWSConfig(brti_history_seconds=1200)
    feed = BRTIFeed(cfg, KalshiClient(base_url="https://api.elections.kalshi.com/trade-api/v2"))
    async with feed._lock:
        from kalshi_bot.brti_ws.feed import BRTITick

        feed._ticks.extend(
            [
                BRTITick(ts_ms=1_000_000, price=100.0),
                BRTITick(ts_ms=1_060_000, price=106.0),
                BRTITick(ts_ms=1_120_000, price=112.0),
            ]
        )
    assert await feed.sma(120) == pytest.approx(106.0)
    assert await feed.price_lagged(120) == pytest.approx(100.0)
    assert await feed.latest() is not None


def test_brti_15m_yaml_loads():
    cfg = load_yaml_config("config/brti_15m.yaml")
    assert cfg.horizon == "15m"
    assert cfg.brti_ws.series_ticker == "KXBTC15M"
    assert cfg.brti_ws.sma_window_seconds == 300
    assert cfg.execution.dry_run is True
