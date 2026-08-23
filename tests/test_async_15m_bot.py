"""Tests for the async 15-minute orchestrator stack."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from brti_engine import BRTIEngine, BRTIWebSocketManager
from config import HARD_STOP_SEC, PHASE1_END_SEC, PHASE2_END_SEC, BotSettings
from kalshi_client import AsyncKalshiClient
from main import FifteenMinuteOrchestrator, determine_phase, phase1_signal, phase3_fair_yes_cents


def test_brti_engine_open_window():
    engine = BRTIEngine(strike_price=100_000.0)
    engine.update_spot(100_500.0)
    metrics = engine.calculate_probability_metrics(0)
    assert metrics["status"] == "OPEN"
    assert metrics["required_avg_remaining"] == 100_000.0


def test_brti_engine_settlement_hurdle():
    engine = BRTIEngine(strike_price=100_000.0)
    engine.update_spot(101_000.0)
    for i in range(30):
        engine.record_settlement_tick(i)
    metrics = engine.calculate_probability_metrics(29)
    assert metrics["recorded_count"] == 30
    assert metrics["remaining_count"] == 30
    assert metrics["required_avg_remaining"] < 100_000.0


def test_brti_engine_mathematical_certainty():
    engine = BRTIEngine(strike_price=100_000.0)
    engine.update_spot(101_500.0)
    for i in range(50):
        engine.record_settlement_tick(i)
    metrics = engine.calculate_probability_metrics(49)
    assert metrics["mathematical_certainty"] is True


def test_phase_boundaries():
    assert determine_phase(0) == 1
    assert determine_phase(PHASE1_END_SEC - 1) == 1
    assert determine_phase(PHASE1_END_SEC) == 2
    assert determine_phase(PHASE2_END_SEC - 1) == 2
    assert determine_phase(PHASE2_END_SEC) == 3
    assert determine_phase(HARD_STOP_SEC) == 4
    assert determine_phase(HARD_STOP_SEC + 1) == 4


def test_phase1_drift_gate():
    strike = 100_000.0
    assert phase1_signal(100_100.0, strike, 0.0065) is None
    assert phase1_signal(100_700.0, strike, 0.0065) == "UP"
    assert phase1_signal(99_300.0, strike, 0.0065) == "DOWN"


def test_safe_order_size_respects_spread_and_depth():
    size = AsyncKalshiClient.safe_order_size(
        limit_price_cents=45,
        depth_at_price=20,
        spread_cents=5,
        max_contracts=25,
        max_spread_cents=3,
        min_book_depth=5,
        max_price_sweep_cents=2,
    )
    assert size == 0

    size = AsyncKalshiClient.safe_order_size(
        limit_price_cents=45,
        depth_at_price=20,
        spread_cents=2,
        max_contracts=25,
        max_spread_cents=3,
        min_book_depth=5,
        max_price_sweep_cents=2,
    )
    assert size == 20


@pytest.mark.asyncio
async def test_vwap_manager_computes_weighted_average():
    mgr = BRTIWebSocketManager(vwap_interval_ms=50, vwap_lookback_sec=2.0)
    now = time.time()
    mgr._trades.append(type("T", (), {"price": 100.0, "volume": 1.0, "venue": "a", "ts": now})())
    mgr._trades.append(type("T", (), {"price": 102.0, "volume": 3.0, "venue": "b", "ts": now})())
    vwap = await mgr._recompute_vwap()
    assert vwap == pytest.approx(101.5)


def test_phase3_fair_price_certainty():
    metrics = {"mathematical_certainty": True}
    assert phase3_fair_yes_cents(metrics, 101_000, 100_000) == 99
    assert phase3_fair_yes_cents(metrics, 99_000, 100_000) == 1


def test_all_phases_enabled_defaults():
    settings = BotSettings()
    assert settings.phase1_enabled is True
    assert settings.phase2_enabled is True
    assert settings.phase3_enabled is True


@pytest.mark.asyncio
async def test_orchestrator_hard_stop_cancels_orders(monkeypatch):
    settings = BotSettings(dry_run=False, kalshi_api_key_id="test")
    orch = FifteenMinuteOrchestrator(settings)

    now = datetime.now(timezone.utc)
    contract = type(
        "C",
        (),
        {
            "ticker": "KXBTC15M-TEST",
            "strike": 100_000.0,
            "open_time": now - timedelta(seconds=896),
            "close_time": now + timedelta(seconds=4),
            "yes_bid": 0.45,
            "yes_ask": 0.47,
            "no_bid": 0.53,
            "no_ask": 0.55,
            "yes_bid_size": 10.0,
            "yes_ask_size": 10.0,
            "spread_cents": 2,
            "seconds_elapsed": 896.0,
            "seconds_remaining": 4.0,
        },
    )()

    cancelled = {"count": 0}

    async def fake_cancel(ticker=None):
        cancelled["count"] += 1
        return 2

    orch.kalshi.cancel_all_orders = fake_cancel  # type: ignore[method-assign]
    monkeypatch.setattr(type(orch.kalshi), "authenticated", property(lambda self: True))
    state = type(
        "S",
        (),
        {
            "contract": contract,
            "brti": BRTIEngine(100_000),
            "resting_order_ids": [],
        },
    )()
    await orch._hard_stop(state)
    assert cancelled["count"] == 1
