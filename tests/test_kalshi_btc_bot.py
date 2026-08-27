"""Tests for the standalone kalshi_btc_bot.py strategy helpers."""

from __future__ import annotations

import kalshi_btc_bot as bot
from kalshi_btc_bot import DashboardRecorder, MarketImbalanceStrategy


def test_extreme_imbalance_generates_signal_without_momentum(monkeypatch):
    monkeypatch.setattr(bot, "REQUIRE_MOMENTUM", True)
    monkeypatch.setattr(bot, "IMBALANCE_EXTREME_THRESHOLD", 3.0)
    strategy = MarketImbalanceStrategy()
    book = {
        "yes": [[45, 300], [44, 100], [43, 50]],
        "no": [[45, 50], [44, 30], [43, 20]],
    }

    signal = strategy.evaluate_market("TEST", book)

    assert signal is not None
    assert signal.side == "yes"
    assert "book_imbalance" in signal.reason


def test_moderate_imbalance_requires_momentum(monkeypatch):
    monkeypatch.setattr(bot, "REQUIRE_MOMENTUM", True)
    monkeypatch.setattr(bot, "IMBALANCE_RATIO_THRESHOLD", 2.2)
    monkeypatch.setattr(bot, "IMBALANCE_EXTREME_THRESHOLD", 3.0)
    strategy = MarketImbalanceStrategy()
    book = {
        "yes": [[45, 220], [44, 100], [43, 50]],
        "no": [[45, 100], [44, 30], [43, 20]],
    }

    assert strategy.evaluate_market("TEST", book) is None

    for midpoint in [44.0, 45.0, 46.0, 47.0, 48.0]:
        strategy._record_midpoint("TEST", midpoint)

    signal = strategy.evaluate_market("TEST", book)
    assert signal is not None
    assert signal.side == "yes"


def test_imbalance_with_momentum_generates_yes_signal(monkeypatch):
    monkeypatch.setattr(bot, "REQUIRE_MOMENTUM", True)
    strategy = MarketImbalanceStrategy()
    book = {
        "yes": [[45, 300], [44, 100], [43, 50]],
        "no": [[45, 50], [44, 30], [43, 20]],
    }
    for midpoint in [44.0, 45.0, 46.0, 47.0, 48.0]:
        strategy._record_midpoint("TEST", midpoint)

    signal = strategy.evaluate_market("TEST", book)

    assert signal is not None
    assert signal.side == "yes"
    assert signal.limit_price == 46
    assert "book_imbalance" in signal.reason


def test_balanced_book_returns_no_signal():
    strategy = MarketImbalanceStrategy()
    book = {
        "yes": [[45, 100], [44, 100], [43, 100]],
        "no": [[45, 100], [44, 100], [43, 100]],
    }

    assert strategy.evaluate_market("TEST", book) is None


def test_one_sided_book_skipped_by_default(monkeypatch):
    monkeypatch.setattr(bot, "ALLOW_ONE_SIDED_BOOKS", False)
    strategy = MarketImbalanceStrategy()
    book = {
        "yes": [],
        "no": [[45, 500], [44, 100], [43, 50]],
    }

    assert strategy.evaluate_market("TEST", book) is None


def test_one_sided_no_book_in_band_generates_signal_when_enabled(monkeypatch):
    monkeypatch.setattr(bot, "ALLOW_ONE_SIDED_BOOKS", True)
    strategy = MarketImbalanceStrategy()
    book = {
        "yes": [],
        "no": [[45, 500], [44, 100], [43, 50]],
    }

    signal = strategy.evaluate_market("TEST", book)

    assert signal is not None
    assert signal.side == "no"
    assert "one_sided_no" in signal.reason


def test_settlement_journal_records_win_outcome():
    dash = DashboardRecorder(mode="paper")
    dash.record_settlement(
        "KXBTC-TEST-B78750",
        "yes",
        2.35,
        True,
        market_result="yes",
        count=5,
        entry_price=27,
        cost_basis=1.35,
        payout=5.0,
    )
    entry = dash.journal[0]
    assert entry["kind"] == "settle"
    assert entry["won"] is True
    assert entry["outcome"] == "WIN"
    assert entry["detail"]["marketResult"] == "yes"
    assert entry["detail"]["pnl"] == 2.35


def test_settlement_journal_records_loss_outcome():
    dash = DashboardRecorder(mode="paper")
    dash.record_settlement(
        "KXBTC-TEST-B78750",
        "yes",
        -1.35,
        False,
        market_result="no",
        count=5,
        entry_price=27,
        cost_basis=1.35,
        payout=0.0,
    )
    entry = dash.journal[0]
    assert entry["won"] is False
    assert entry["outcome"] == "LOSS"
    assert entry["detail"]["marketResult"] == "no"
