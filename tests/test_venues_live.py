"""Integration-ish tests against public Kalshi / Polymarket APIs (read-only)."""

from __future__ import annotations

import pytest

from kalshi_bot.strategies.mispricing import extract_strike
from kalshi_bot.venues.kalshi import KalshiClient, KalshiMarket
from kalshi_bot.venues.polymarket import PolymarketClient
from datetime import datetime, timezone, timedelta


@pytest.fixture
def kalshi():
    client = KalshiClient(base_url="https://api.elections.kalshi.com/trade-api/v2")
    yield client
    client.close()


def test_kalshi_fetches_kxbtc15m(kalshi):
    mkts = kalshi.get_markets("KXBTC15M", status="open", limit=5)
    assert isinstance(mkts, list)
    # Market may be empty between windows; if present, validate shape
    for m in mkts:
        assert m.ticker.startswith("KXBTC15M")
        assert m.yes_ask >= 0
        assert m.close_time.tzinfo is not None


def test_kalshi_fetches_kxbtcd(kalshi):
    mkts = kalshi.get_markets("KXBTCD", status="open", limit=10)
    assert isinstance(mkts, list)
    if mkts:
        assert any(m.floor_strike for m in mkts)


def test_extract_strike_from_floor():
    m = KalshiMarket(
        ticker="KXBTCD-X",
        event_ticker="X",
        series="KXBTCD",
        title="Bitcoin price?",
        status="active",
        yes_bid=0.1,
        yes_ask=0.2,
        no_bid=0.8,
        no_ask=0.9,
        yes_bid_size=1,
        yes_ask_size=1,
        no_bid_size=1,
        no_ask_size=1,
        floor_strike=73799.99,
        close_time=datetime.now(timezone.utc) + timedelta(hours=1),
        open_time=None,
        rules_primary="above 73799.99",
        strike_type="greater",
        volume=0,
    )
    assert extract_strike(m, spot=65000) == pytest.approx(73799.99)


def test_polymarket_btc_15m_readable():
    client = PolymarketClient()
    try:
        mkt = client.get_btc_15m()
        # May be None briefly at window boundaries
        if mkt:
            assert mkt.up_price > 0
            assert mkt.down_price > 0
            assert "btc-updown-15m" in mkt.slug
    finally:
        client.close()