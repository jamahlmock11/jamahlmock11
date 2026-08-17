"""Tests for trade journal + dashboard API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from kalshi_bot.dashboard.app import create_app
from kalshi_bot.dashboard.requirements import build_reversal_status
from kalshi_bot.domain import DecisionAction, DecisionResult, Direction
from kalshi_bot.journal import TradeJournal
from kalshi_bot.models.probability import Confidence, EdgeSignal, Side


def test_journal_roundtrip(tmp_path: Path):
    db = tmp_path / "t.db"
    j = TradeJournal(db)
    scan_id = j.log_scan(
        spot=65000,
        ibit=36.5,
        iv_atm=0.55,
        markets_scanned=10,
        signal_count=2,
        mode="DRY-RUN",
    )
    sig = EdgeSignal(
        ticker="KXBTCD-TEST",
        series="KXBTCD",
        side=Side.YES,
        kalshi_prob=0.22,
        options_prob=0.378,
        edge_pp=15.8,
        confidence=Confidence.HIGH,
        spread_cents=2,
        book_usd=50,
        strike=60000,
        spot=65000,
        iv=0.55,
        t_years=0.01,
        reason="test",
    )
    j.log_signal(scan_id, sig, traded=True)
    tid = j.log_trade(
        strategy="mispricing",
        ticker="KXBTCD-TEST",
        side="yes",
        count=10,
        price=0.22,
        notional=2.2,
        edge=15.8,
        confidence="HIGH",
        dry_run=True,
        ok=True,
        detail="dry",
        payload={"x": 1},
    )
    assert tid
    stats = j.stats()
    assert stats["trades"] == 1
    assert stats["signals"] == 1
    assert stats["dry_trades"] == 1
    assert len(j.recent_trades()) == 1


def test_dashboard_api(tmp_path: Path):
    db = tmp_path / "dash.db"
    j = TradeJournal(db)
    j.log_trade(
        strategy="cross_venue_arb",
        ticker="KXBTC15M-X",
        side="YES",
        count=5,
        price=0.98,
        notional=4.9,
        edge=2.0,
        confidence="ARB",
        dry_run=True,
        ok=True,
        detail="arb",
    )
    now = datetime.now(timezone.utc)
    cycle = SimpleNamespace(
        timestamp=now,
        data_health="FAILED",
        reason="primary BRTI unavailable",
        market=SimpleNamespace(
            ticker="KXBTC15M-X",
            strike=65000,
            expiration=now + timedelta(minutes=5),
            yes_bid=0.48,
            yes_ask=0.52,
            no_bid=0.48,
            no_ask=0.52,
            current_position=None,
        ),
        benchmark=None,
        features=None,
        forecast=None,
        regime=None,
        decision=DecisionResult(
            action=DecisionAction.NO_TRADE,
            reason="primary BRTI unavailable",
            gate_failures=(),
            current_direction=Direction.FLAT,
            predicted_direction=Direction.FLAT,
            trade_direction=Direction.FLAT,
        ),
    )
    j.log_decision(cycle, dry_run=True)
    client = TestClient(create_app(db))
    assert client.get("/api/health").json()["ok"] is True
    trades = client.get("/api/trades").json()["trades"]
    assert len(trades) == 1
    decisions = client.get("/api/decisions").json()["decisions"]
    assert len(decisions) == 1
    assert decisions[0]["action"] == "NO_TRADE"
    assert "requirements" in decisions[0]
    assert decisions[0]["blocking_summary"]
    assert client.get("/").status_code == 200
    assert "edge" in client.get("/").text.lower()
    assert client.get("/static/styles.css").status_code == 200
    analytics = client.get("/api/analytics").json()
    assert "win_rate_by_time_remaining" in analytics
    assert "total_trades" in analytics


def test_build_reversal_status_signal_only():
    status = build_reversal_status(
        {
            "lag_reversal": {
                "enabled": True,
                "entry_enabled": False,
                "signal_only": True,
            },
            "reversal_signal": {
                "score": 56.0,
                "tier": "watch",
                "active": True,
                "setup": "DOWN stall → yes · lag +21.6% · Δprob -2.5%",
                "summary": "watch reversal setup",
            },
        }
    )
    assert status["enabled"]
    assert status["signal_only"]
    assert status["active"]
    assert status["mode_label"] == "Signal only"
    assert status["score"] == 56.0


def test_dashboard_reversal_status_in_decisions(tmp_path: Path):
    db = tmp_path / "rev.db"
    j = TradeJournal(db)
    now = datetime.now(timezone.utc)
    cycle = SimpleNamespace(
        timestamp=now,
        data_health="OK",
        reason="no edge",
        market=SimpleNamespace(
            ticker="KXBTC15M-X",
            strike=65000,
            expiration=now + timedelta(minutes=5),
            yes_bid=0.04,
            yes_ask=0.05,
            no_bid=0.95,
            no_ask=0.96,
            current_position=None,
        ),
        benchmark=None,
        features=None,
        forecast=None,
        regime=None,
        decision=DecisionResult(
            action=DecisionAction.NO_TRADE,
            reason="no edge",
            gate_failures=(),
            current_direction=Direction.DOWN,
            predicted_direction=Direction.DOWN,
            trade_direction=Direction.FLAT,
        ),
    )
    j.log_decision(
        cycle,
        dry_run=False,
        payload={
            "lag_reversal": {"enabled": True, "entry_enabled": False, "signal_only": True},
            "reversal_signal": {
                "score": 72.0,
                "tier": "reversal_candidate",
                "active": True,
                "setup": "DOWN stall → yes",
                "summary": "candidate reversal",
            },
        },
    )
    client = TestClient(create_app(db))
    decision = client.get("/api/decisions").json()["decisions"][0]
    assert decision["reversal_status"]["active"]
    assert decision["reversal_status"]["tier_label"] == "Candidate"
    lag_req = next(r for r in decision["requirements"] if r["id"] == "lag_reversal")
    assert lag_req["status"] == "pass"
