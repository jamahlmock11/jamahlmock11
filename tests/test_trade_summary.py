"""Tests for trade summary parsing."""

from __future__ import annotations

from kalshi_bot.dashboard.trade_summary import (
    aggregate_trade_stats,
    parse_trade_detail,
    summarize_trade,
)


def test_parse_trade_detail_exit_pnl():
    detail = "[LIVE] EXIT 1 YES KXBTC15M-26AUG090400-00 @13¢ pnl=$-0.01"
    parsed = parse_trade_detail(detail)
    assert parsed["action_label"] == "EXIT"
    assert parsed["mode_label"] == "LIVE"
    assert parsed["pnl_usd"] == -0.01
    assert parsed["price_cents"] == 13


def test_summarize_trade_buy():
    row = {
        "ts": 1.0,
        "strategy": "forecast",
        "ticker": "KXBTC15M-TEST",
        "side": "yes",
        "count": 1,
        "price": 0.25,
        "notional": 0.25,
        "edge": 22.3,
        "confidence": "ENSEMBLE",
        "dry_run": 0,
        "ok": 1,
        "detail": "[LIVE] BUY_UP 1 KXBTC15M-TEST @25¢ edge=22.3%",
        "payload": "{}",
    }
    trade = summarize_trade(row, horizon="15m")
    assert trade["horizon"] == "15m"
    assert "buy up" in trade["summary"].lower()
    assert trade["edge_pct"] == 22.3


def test_aggregate_trade_stats():
    trades = [
        summarize_trade(
            {
                "strategy": "forecast_exit",
                "ticker": "KXBTC15M-X",
                "side": "yes",
                "count": 1,
                "price": 0.2,
                "notional": 0.2,
                "edge": 0,
                "dry_run": 0,
                "ok": 1,
                "detail": "[LIVE] EXIT 1 YES KXBTC15M-X @20¢ pnl=$-0.05",
            },
            horizon="15m",
        )
    ]
    stats = aggregate_trade_stats(trades)
    assert stats["exits"] == 1
    assert stats["closed_pnl_usd"] == -0.05
