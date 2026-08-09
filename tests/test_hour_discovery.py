"""Tests for 1-hour KXBTCD market discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kalshi_bot.config import HourStrategyConfig
from kalshi_bot.hour.discovery import (
    HourDiscoveryConfig,
    discover_hour_market,
    filter_hourly_markets,
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
