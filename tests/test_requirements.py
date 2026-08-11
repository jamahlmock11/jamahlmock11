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
                    "required": 0.20,
                }
            ]
        ),
        "payload": json.dumps({"config": {"min_edge": 0.20}}),
        "position": json.dumps(None),
    }
    base.update(overrides)
    return base


def test_no_trade_marks_edge_as_blocking():
    result = build_trade_requirements(_row())
    edge = next(r for r in result["requirements"] if r["id"] == "edge")
    assert edge["blocking"] is True
    assert edge["status"] == "fail"
    assert "Minimum edge" in result["blocking_summary"]
    assert result["blocking_labels"] == ["Minimum edge"]


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
