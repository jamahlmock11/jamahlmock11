"""Tests for Edge Desk scanner layout API."""

from __future__ import annotations

import pytest

from kalshi_bot.dashboard.assessments import build_assessment, latest_assessments
from kalshi_bot.dashboard.rules import active_edge_rules


def test_active_edge_rules_has_expected_keys():
    payload = active_edge_rules(mode="LIVE")
    keys = {r["key"] for r in payload["rules"]}
    assert "Mode" in keys
    assert "Edge (std)" in keys
    assert "Ensemble" in keys
    assert "20¢" in payload["summary"]


def test_build_assessment_maps_blocker_on_skip():
    row = {
        "horizon": "15m",
        "ticker": "KXBTC15M-X",
        "seconds_remaining": 12.0,
        "yes_bid": 0.99,
        "yes_ask": 1.0,
        "edge": 0.0,
        "signal_agreement": 1.0,
        "action": "NO_TRADE",
        "pass_count": 10,
        "fail_count": 2,
        "primary_blocker": "Minimum edge: need 20¢ more (0¢ have · 20¢ need)",
    }
    assessment = build_assessment(row)
    assert assessment["asset"] == "BTC"
    assert assessment["book"] == "UP 100%"
    assert assessment["rec"] == "skip"
    assert assessment["action"] == "block"
    assert "Minimum edge" in assessment["blocker"]
    assert assessment["tau_left_min"] == 0.2


def test_latest_assessments_one_per_horizon():
    rows = [
        {"horizon": "15m", "action": "NO_TRADE", "seconds_remaining": 100, "yes_ask": 0.5, "yes_bid": 0.48},
        {"horizon": "1h", "action": "NO_TRADE", "seconds_remaining": 2000, "yes_ask": 0.8, "yes_bid": 0.78},
        {"horizon": "15m", "action": "BUY_UP", "seconds_remaining": 50, "yes_ask": 0.6, "yes_bid": 0.58},
    ]
    latest = latest_assessments(rows)
    horizons = {a["horizon"] for a in latest}
    assert horizons == {"15m", "1h"}
    by_hz = {a["horizon"]: a for a in latest}
    assert by_hz["15m"]["tau_left_min"] == pytest.approx(1.7, abs=0.2)
