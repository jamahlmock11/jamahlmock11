"""Tests for alternative strategy modules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kalshi_bot.config import MeanReversionConfig, OrderbookSkewConfig, SpotLagArbConfig
from kalshi_bot.data.spot_hub import SpotPriceHub, SpotTick
from kalshi_bot.domain import ContractSide, MarketSnapshot
from kalshi_bot.market.orderbook import parse_orderbook_fp, skew_top_n
from kalshi_bot.strategies.mean_reversion import evaluate_mean_reversion
from kalshi_bot.strategies.orderbook_skew import evaluate_orderbook_skew
from kalshi_bot.strategies.spot_lag_arb import evaluate_spot_lag

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def book_skewed(yes_ask: float = 0.55):
    yes_bid = yes_ask - 0.02
    no_bid = 1.0 - yes_ask
    return parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_bid:.4f}", "200"], [f"{yes_bid - 0.02:.4f}", "100"]],
                "no_dollars": [[f"{no_bid:.4f}", "40"]],
            }
        },
        timestamp=NOW,
    )


def book_cheap_yes(yes_ask: float = 0.12):
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


def market_snapshot(book, *, minutes: float = 2.0):
    return MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="CF Benchmarks BRTI",
        strike=65000.0,
        expiration=NOW + timedelta(minutes=minutes),
        open_time=NOW - timedelta(minutes=13),
        reference="BRTI",
        orderbook=book,
    )


class StubSpotHub(SpotPriceHub):
    def __init__(self, move: float, price: float = 65100.0):
        super().__init__(poll_interval_sec=60)
        self._move = move
        self._latest = SpotTick(price, "stub", NOW)

    def move_since(self, seconds: float) -> float | None:
        return self._move


def test_skew_top_n_positive_on_bid_heavy_book():
    assert skew_top_n(book_skewed(0.55), n=5) > 0


def test_orderbook_skew_fires_in_late_window():
    signal = evaluate_orderbook_skew(
        market_snapshot(book_skewed(0.55), minutes=2),
        cfg=OrderbookSkewConfig(enabled=True, min_skew=0.1, min_z_distance=0.0),
        seconds_remaining=120,
        spot_price=65200,
    )
    assert signal is not None
    assert signal.strategy == "orderbook_skew"


def test_spot_lag_detects_move_and_lag():
    hub = StubSpotHub(move=80.0, price=65150.0)
    result = evaluate_spot_lag(
        market_snapshot(book_skewed(0.45), minutes=10),
        spot_hub=hub,
        cfg=SpotLagArbConfig(enabled=True, min_spot_move_usd=50, min_implied_lag=0.01),
        seconds_remaining=600,
    )
    assert result.signal is not None
    assert result.signal.strategy == "spot_lag"


def test_mean_reversion_posts_maker_on_cheap_yes():
    signals = evaluate_mean_reversion(
        market_snapshot(book_cheap_yes(0.12), minutes=8),
        cfg=MeanReversionConfig(enabled=True),
        open_orders=(),
        position=None,
    )
    assert signals
    assert signals[0].side is ContractSide.YES
    assert signals[0].time_in_force == "good_til_canceled"
