"""Tests for 1-hour KXBTCD market discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kalshi_bot.config import HourStrategyConfig
from kalshi_bot.hour.discovery import (
    HourDiscoveryConfig,
    discover_all_hour_markets,
    discover_hour_market,
    filter_hourly_markets,
    markets_for_expiration,
    select_active_hour_strike_markets,
    select_nearest_strike_markets,
)
from kalshi_bot.market.orderbook import parse_orderbook_fp

NOW = datetime(2026, 8, 9, 9, 30, tzinfo=timezone.utc)


@dataclass
class FakeMarket:
    ticker: str
    status: str = "active"
    market_type: str = "binary"
    floor_strike: float = 65000.0
    open_time: datetime = NOW - timedelta(minutes=30)
    close_time: datetime = NOW + timedelta(minutes=30)
    rules_primary: str = (
        "If the simple average of the sixty seconds of CF Benchmarks' "
        "Bitcoin Real-Time Index (BRTI) before settlement is above the strike, "
        "then the market resolves to Yes."
    )


def _book():
    return parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.48", "100"]],
                "no_dollars": [["0.50", "100"]],
            }
        },
        timestamp=NOW,
    )


def test_filter_hourly_markets_accepts_kxbtcd_binary_duration():
    hour_cfg = HourStrategyConfig(series_ticker="KXBTCD")
    config = HourDiscoveryConfig(hour=hour_cfg)
    markets = [
        FakeMarket(ticker="KXBTCD-26AUG0906-T64799.99", floor_strike=64799.99),
        FakeMarket(
            ticker="KXBTC15M-26AUG090630-00",
            market_type="binary",
            open_time=NOW - timedelta(minutes=5),
            close_time=NOW + timedelta(minutes=10),
        ),
    ]
    filtered = filter_hourly_markets(markets, config=config)
    assert [m.ticker for m in filtered] == ["KXBTCD-26AUG0906-T64799.99"]


def test_select_nearest_strike_markets_prefers_atm():
    markets = [
        FakeMarket(ticker="KXBTCD-A", floor_strike=70000),
        FakeMarket(ticker="KXBTCD-B", floor_strike=64800),
        FakeMarket(ticker="KXBTCD-C", floor_strike=64600),
    ]
    selected = select_nearest_strike_markets(markets, 64750, count=2)
    assert [m.ticker for m in selected] == ["KXBTCD-B", "KXBTCD-C"]


def test_discover_hour_market_finds_contract_outside_entry_window():
    hour_cfg = HourStrategyConfig(
        series_ticker="KXBTCD",
        max_entry_seconds_remaining=1200,
        contract_duration_seconds=3600,
    )
    config = HourDiscoveryConfig(hour=hour_cfg)
    markets = [
        FakeMarket(
            ticker="KXBTCD-26AUG0907",
            floor_strike=64800,
            open_time=NOW - timedelta(minutes=30),
            close_time=NOW + timedelta(minutes=30),
        ),
    ]
    orderbooks = {"KXBTCD-26AUG0907": _book()}
    result = discover_hour_market(
        markets,
        orderbooks=orderbooks,
        now=NOW,
        config=config,
        reference_price=64790,
    )
    assert result.market is not None
    assert result.market.ticker == "KXBTCD-26AUG0907"


def test_discover_all_hour_markets_returns_every_strike_on_active_expiration():
    hour_cfg = HourStrategyConfig(series_ticker="KXBTCD", contract_duration_seconds=3600)
    config = HourDiscoveryConfig(hour=hour_cfg)
    exp = NOW + timedelta(minutes=30)
    markets = [
        FakeMarket(ticker="KXBTCD-S1", floor_strike=64000, close_time=exp),
        FakeMarket(ticker="KXBTCD-S2", floor_strike=64500, close_time=exp),
        FakeMarket(ticker="KXBTCD-S3", floor_strike=65000, close_time=exp),
        FakeMarket(ticker="KXBTCD-S4", floor_strike=65500, close_time=exp),
        FakeMarket(ticker="KXBTCD-NEXT", floor_strike=65000, close_time=exp + timedelta(hours=1)),
    ]
    orderbooks = {m.ticker: _book() for m in markets}
    batch = discover_all_hour_markets(
        markets,
        orderbooks=orderbooks,
        now=NOW,
        config=config,
        all_strikes_for_active_hour=True,
    )
    assert batch.expiration == exp
    assert len(batch.markets) == 4
    assert {m.strike for m in batch.markets} == {64000.0, 64500.0, 65000.0, 65500.0}


def test_select_active_hour_strike_markets_ignores_farther_expiration():
    hour_cfg = HourStrategyConfig(series_ticker="KXBTCD", contract_duration_seconds=3600)
    config = HourDiscoveryConfig(hour=hour_cfg)
    exp = NOW + timedelta(minutes=30)
    later = exp + timedelta(hours=1)
    markets = [
        FakeMarket(ticker="KXBTCD-NEAR-LOW", floor_strike=70000, close_time=exp),
        FakeMarket(ticker="KXBTCD-NEAR-HIGH", floor_strike=71000, close_time=exp),
        FakeMarket(ticker="KXBTCD-FAR-ATM", floor_strike=64750, close_time=later),
    ]
    filtered = filter_hourly_markets(markets, config=config)
    active_exp, active = select_active_hour_strike_markets(
        filtered,
        NOW,
        minimum_seconds_remaining=hour_cfg.min_seconds_remaining,
        maximum_seconds_remaining=hour_cfg.contract_duration_seconds,
    )
    assert active_exp == exp
    assert {m.ticker for m in active} == {"KXBTCD-NEAR-LOW", "KXBTCD-NEAR-HIGH"}


def test_discover_hour_market_finds_atm_kxbtcd_contract():
    hour_cfg = HourStrategyConfig(series_ticker="KXBTCD", max_entry_seconds_remaining=3600)
    config = HourDiscoveryConfig(hour=hour_cfg)
    markets = [
        FakeMarket(ticker="KXBTCD-FAR", floor_strike=71000),
        FakeMarket(ticker="KXBTCD-ATM", floor_strike=64800),
    ]
    orderbooks = {
        "KXBTCD-FAR": _book(),
        "KXBTCD-ATM": _book(),
    }
    result = discover_hour_market(
        markets,
        orderbooks=orderbooks,
        now=NOW,
        config=config,
        reference_price=64790,
    )
    assert result.market is not None
    assert result.market.ticker == "KXBTCD-ATM"
