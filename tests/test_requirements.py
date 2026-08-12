"""Tests for dashboard trade requirement enrichment."""

from __future__ import annotations

import json

from kalshi_bot.dashboard.requirements import build_trade_requirements, enrich_decision


def _row(**overrides):
    base = {
        "action": "NO_TRADE",
        "data_health": "HEALTHY",
        "seconds_remaining": 300.0,
        "edge": 0.12,
        "executable_price": 0.22,
        "signal_agreement": 0.55,
        "confidence": 0.62,
        "benchmark_source": "BRTI",
        "gate_failures": json.dumps(
            [
                {
                    "gate": "minimum_edge",
                    "reason": "edge below threshold",
                    "observed": 0.12,
                    "required": 0.15,
                }
            ]
        ),
        "payload": json.dumps({"config": {"min_edge": 0.15}}),
        "position": json.dumps(None),
    }
    base.update(overrides)
    return base


def test_no_trade_marks_edge_as_blocking():
    result = build_trade_requirements(_row())
    edge = next(r for r in result["requirements"] if r["id"] == "edge")
    assert edge["blocking"] is True
    assert edge["status"] == "fail"
    assert "need 3¢ more" in edge["detail"]
    assert "Minimum edge" in result["blocking_summary"]
    assert "need 3¢ more" in result["blocking_summary"]
    assert result["edge_gap_cents"] == 3.0
    assert result["primary_blocker"].startswith("Minimum edge:")


def test_hour_edge_gate_maps_to_edge_requirement():
    row = _row(
        gate_failures=json.dumps(
            [
                {
                    "gate": "edge",
                    "reason": "no executable edge available",
                    "observed": 0.05,
                    "required": 0.15,
                }
            ]
        )
    )
    result = build_trade_requirements(row)
    edge = next(r for r in result["requirements"] if r["id"] == "edge")
    assert edge["blocking"] is True
    assert "need 10¢ more" in result["primary_blocker"]


def test_spread_failure_shows_actual_values():
    row = _row(
        edge=0.25,
        gate_failures=json.dumps(
            [
                {
                    "gate": "yes_spread",
                    "reason": "YES spread is missing or too wide",
                    "observed": 0.14,
                    "required": 0.12,
                }
            ]
        ),
    )
    result = build_trade_requirements(row)
    assert "YES spread: 14¢ spread · max 12¢" in result["blocking_summary"]
    spread = next(r for r in result["requirements"] if r["id"] == "spread")
    assert spread["blocking"] is True
    assert "14¢" in spread["detail"]


def test_brti_failure_maps_to_requirement():
    row = _row(
        data_health="FAILED",
        gate_failures=json.dumps(
            [{"gate": "primary_brti", "reason": "stale", "observed": None, "required": None}]
        ),
        reason="primary BRTI unavailable",
    )
    result = build_trade_requirements(row)
    brti = next(r for r in result["requirements"] if r["id"] == "brti")
    assert brti["blocking"] is True
    assert "Official BRTI feed" in result["blocking_summary"]


def test_enrich_decision_adds_requirements_fields():
    enriched = enrich_decision(_row())
    assert "requirements" in enriched
    assert enriched["blocking_summary"]
    assert enriched["pass_count"] >= 1
    assert enriched["fail_count"] >= 1
