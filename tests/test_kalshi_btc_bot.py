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


def test_sort_tradeable_prioritizes_near_atm_before_otm_band():
    spot = 87_800
    tradeable = [
        (30.0, {"ticker": "KXBTC-TEST-B75800"}),   # OTM band
        (30.0, {"ticker": "KXBTC-TEST-B87750"}),   # near ATM
        (30.0, {"ticker": "KXBTC-TEST-B95000"}),   # far from spot
    ]
    ordered = bot.sort_tradeable_for_scan(tradeable, btc_spot=spot)
    assert ordered[0][1]["ticker"] == "KXBTC-TEST-B87750"
    assert ordered[1][1]["ticker"] == "KXBTC-TEST-B75800"
    assert ordered[2][1]["ticker"] == "KXBTC-TEST-B95000"


def test_is_extreme_quote_detects_one_sided_99_books():
    assert bot.is_extreme_quote({"yesBid": 0, "yesAsk": 0, "noBid": 99, "noAsk": 0}) is True
    assert bot.is_extreme_quote({"yesBid": 45, "yesAsk": 47, "noBid": 53, "noAsk": 55}) is False


def test_sort_market_rows_puts_extreme_quotes_last_unless_candidate():
    rows = [
        {"ticker": "EXTREME", "isExtremeQuote": True, "isAtm": True, "distFromSpot": 50},
        {"ticker": "ATM", "isExtremeQuote": False, "isAtm": True, "distFromSpot": 100},
        {"ticker": "SIGNAL", "isExtremeQuote": True, "isAtm": True, "distFromSpot": 200, "tradeCandidate": True},
    ]
    ordered = bot.sort_market_rows_for_display(rows, trade_candidates={"SIGNAL"}, open_tickers=set())
    assert [row["ticker"] for row in ordered] == ["SIGNAL", "ATM", "EXTREME"]


def test_select_dashboard_markets_skips_threshold_tickers():
    tradeable = [
        (30.0, {"ticker": "KXBTC-TEST-T87800"}),
        (30.0, {"ticker": "KXBTC-TEST-B87750"}),
        (30.0, {"ticker": "KXBTC-TEST-B87650"}),
    ]
    selected = bot.select_dashboard_markets(tradeable, 87_800, limit=2)
    tickers = [m["ticker"] for m in selected]
    assert "KXBTC-TEST-T87800" not in tickers
    assert tickers[0] == "KXBTC-TEST-B87750"


def test_place_order_uses_v2_yes_body(monkeypatch):
    captured = {}

    def fake_request(self, method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = kwargs.get("json_body")
        return {"order_id": "abc"}

    monkeypatch.setattr(bot.KalshiClient, "_request", fake_request)
    client = bot.KalshiClient.__new__(bot.KalshiClient)
    client.place_order("KXBTC-TEST-B79000", "yes", 3, 63, "limit")

    assert captured["path"] == "/portfolio/events/orders"
    assert captured["body"]["side"] == "bid"
    assert captured["body"]["price"] == "0.6300"
    assert captured["body"]["count"] == "3.00"


def test_place_order_uses_v2_no_body(monkeypatch):
    captured = {}

    def fake_request(self, method, path, **kwargs):
        captured["body"] = kwargs.get("json_body")
        return {"order_id": "abc"}

    monkeypatch.setattr(bot.KalshiClient, "_request", fake_request)
    client = bot.KalshiClient.__new__(bot.KalshiClient)
    client.place_order("KXBTC-TEST-B79000", "no", 2, 62, "limit")

    assert captured["body"]["side"] == "ask"
    assert captured["body"]["price"] == "0.3800"


def test_restore_persisted_state_reloads_journal_and_filters_scans(tmp_path, monkeypatch):
    status_path = tmp_path / "1h_bot_status.json"
    status_path.write_text(
        """
        {
          "journal": [{"id": "j1", "kind": "fill", "text": "filled"}],
          "logs": [
            {"id": "l1", "kind": "fill", "text": "filled"},
            {"id": "l2", "kind": "scan", "text": "noise"}
          ],
          "equityHistory": [{"t": 42, "v": 21.5}],
          "winsToday": 2,
          "lossesToday": 1,
          "dayPnl": 1.25,
          "bankroll": 21.25,
          "peakEquity": 22.0,
          "positions": []
        }
        """
    )
    monkeypatch.setattr(bot, "_status_file_path", lambda: status_path)

    risk = bot.RiskManager()
    dash = DashboardRecorder(mode="paper")
    bot.restore_persisted_state(risk, dash, client=object(), mode="paper")

    assert len(dash.journal) == 1
    assert dash.journal[0]["kind"] == "fill"
    assert [row["kind"] for row in dash.logs] == ["fill"]
    assert dash.equity_history == [{"t": 42, "v": 21.5}]
    assert dash.wins_today == 2
    assert dash.losses_today == 1
    assert risk.realized_pnl_today == 1.25
    assert risk.bankroll == 21.25
    assert dash.peak_equity == 22.0
