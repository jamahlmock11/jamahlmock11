"""Tests for cross-venue arb detection and risk sizing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from kalshi_bot.config import AppConfig, CrossVenueConfig, ExecutionConfig
from kalshi_bot.execution.risk import RiskManager
from kalshi_bot.models.probability import Confidence, EdgeSignal, Side
from kalshi_bot.strategies.cross_venue_arb import CrossVenueArbScanner
from kalshi_bot.venues.kalshi import KalshiMarket
from kalshi_bot.venues.polymarket import PolyMarket, current_15m_slug


def _kalshi_mkt(yes_ask=0.48, no_ask=0.53):
    now = datetime.now(timezone.utc)
    return KalshiMarket(
        ticker="KXBTC15M-TEST-30",
        event_ticker="KXBTC15M-TEST",
        series="KXBTC15M",
        title="BTC price up in next 15 mins?",
        status="active",
        yes_bid=yes_ask - 0.01,
        yes_ask=yes_ask,
        no_bid=no_ask - 0.01,
        no_ask=no_ask,
        yes_bid_size=100,
        yes_ask_size=100,
        no_bid_size=100,
        no_ask_size=100,
        floor_strike=65000.0,
        close_time=now + timedelta(minutes=10),
        open_time=now - timedelta(minutes=5),
        rules_primary="BRTI up",
        strike_type="greater_or_equal",
        volume=1000,
    )


def test_arb_when_pair_under_one():
    kalshi = MagicMock()
    kalshi.get_markets.return_value = [_kalshi_mkt(yes_ask=0.45, no_ask=0.56)]
    poly = MagicMock()
    poly.get_btc_15m.return_value = PolyMarket(
        slug=current_15m_slug(),
        question="BTC up/down",
        end_ts=datetime.now(timezone.utc).timestamp() + 600,
        up_token_id="up",
        down_token_id="down",
        up_price=0.55,
        down_price=0.50,  # 0.45 + 0.50 = 0.95 < 0.99
    )
    scanner = CrossVenueArbScanner(kalshi, poly, CrossVenueConfig(max_pair_cost=0.99))
    opps = scanner.scan()
    assert opps
    assert opps[0].pair_cost < 1.0
    assert opps[0].edge > 0.01


def test_no_arb_when_efficient():
    kalshi = MagicMock()
    kalshi.get_markets.return_value = [_kalshi_mkt(yes_ask=0.51, no_ask=0.51)]
    poly = MagicMock()
    poly.get_btc_15m.return_value = PolyMarket(
        slug="x",
        question="q",
        end_ts=datetime.now(timezone.utc).timestamp() + 600,
        up_token_id="u",
        down_token_id="d",
        up_price=0.51,
        down_price=0.51,
    )
    scanner = CrossVenueArbScanner(kalshi, poly, CrossVenueConfig(max_pair_cost=0.99))
    assert scanner.scan() == []


def test_risk_sizes_high_more_than_low():
    cfg = AppConfig(execution=ExecutionConfig(max_position_usd=100, only_tiers=["HIGH", "MEDIUM", "LOW"]))
    risk = RiskManager(cfg)
    base = dict(
        ticker="T",
        series="KXBTCD",
        kalshi_prob=0.22,
        options_prob=0.378,
        spread_cents=2.0,
        book_usd=200.0,
        strike=60000,
        spot=65000,
        iv=0.55,
        t_years=1 / 24,
        reason="test",
    )
    high = EdgeSignal(side=Side.YES, edge_pp=15.8, confidence=Confidence.HIGH, **base)
    low = EdgeSignal(side=Side.YES, edge_pp=6.0, confidence=Confidence.LOW, **base)
    assert risk.size_mispricing(high) > risk.size_mispricing(low)
    assert risk.size_mispricing(high) > 0


def test_pass_gets_zero_size():
    cfg = AppConfig()
    risk = RiskManager(cfg)
    sig = EdgeSignal(
        ticker="T",
        series="KXBTCD",
        side=Side.YES,
        kalshi_prob=0.5,
        options_prob=0.52,
        edge_pp=2.0,
        confidence=Confidence.PASS,
        spread_cents=1,
        book_usd=100,
        strike=1,
        spot=1,
        iv=0.5,
        t_years=0.01,
        reason="x",
    )
    assert risk.size_mispricing(sig) == 0


def test_15m_slug_format():
    slug = current_15m_slug(1_786_173_300)
    assert slug.startswith("btc-updown-15m-")
    assert slug.endswith("00")