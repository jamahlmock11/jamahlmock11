"""Tests for the 1-hour bot dashboard status API."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_bot.dashboard.app import create_app
from kalshi_bot.dashboard.hour_bot_status import HourBotStatus, apply_control_update


def test_hour_bot_status_default(tmp_path: Path, monkeypatch):
    status_path = tmp_path / "status.json"
    control_path = tmp_path / "control.json"
    monkeypatch.setenv("HOUR_BOT_STATUS_PATH", str(status_path))
    monkeypatch.setenv("HOUR_BOT_CONTROL_PATH", str(control_path))

    client = TestClient(create_app())
    payload = client.get("/api/1h-bot/status").json()
    assert "bankroll" in payload
    assert payload["series"] == "KXBTC"


def test_hour_bot_control_roundtrip(tmp_path: Path, monkeypatch):
    status_path = tmp_path / "status.json"
    control_path = tmp_path / "control.json"
    monkeypatch.setenv("HOUR_BOT_STATUS_PATH", str(status_path))
    monkeypatch.setenv("HOUR_BOT_CONTROL_PATH", str(control_path))

    client = TestClient(create_app())
    resp = client.post("/api/1h-bot/control", json={"running": False, "estop": True})
    assert resp.status_code == 200
    assert resp.json()["estop"] is True
    assert resp.json()["running"] is False


def test_hour_bot_status_reads_written_file(tmp_path: Path, monkeypatch):
    status_path = tmp_path / "status.json"
    control_path = tmp_path / "control.json"
    monkeypatch.setenv("HOUR_BOT_STATUS_PATH", str(status_path))
    monkeypatch.setenv("HOUR_BOT_CONTROL_PATH", str(control_path))

    status = HourBotStatus(bankroll=88.5, dayPnl=1.25, series="KXBTC")
    status.save()

    client = TestClient(create_app())
    payload = client.get("/api/1h-bot/status").json()
    assert payload["bankroll"] == 88.5
    assert payload["dayPnl"] == 1.25

    control = apply_control_update(mode="paper")
    assert control.mode == "paper"
    assert json.loads(control_path.read_text())["mode"] == "paper"
