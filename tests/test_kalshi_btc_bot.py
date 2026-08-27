"""Tests for the standalone kalshi_btc_bot.py strategy helpers."""

from __future__ import annotations

from kalshi_btc_bot import MarketImbalanceStrategy


def test_imbalance_with_momentum_generates_yes_signal():
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
