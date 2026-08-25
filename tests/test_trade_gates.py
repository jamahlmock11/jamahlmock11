"""Tests for proxy/paper decision config and BRTI history persistence."""

from __future__ import annotations

from datetime import datetime, timezone

from kalshi_bot.config import AppConfig, load_yaml_config
from kalshi_bot.domain import BenchmarkQuote, RollingPricePoint
from kalshi_bot.features.history_store import load_history, save_history
from kalshi_bot.strategies.decision import decision_config_from_app


def test_proxy_minimum_edge_matches_strategy_not_hardcoded_25():
    cfg = load_yaml_config("config/default.yaml")
    cfg = cfg.model_copy(
        update={
            "execution": cfg.execution.model_copy(update={"dry_run": True}),
            "data": cfg.data.model_copy(update={"benchmark_mode": "constituent_proxy"}),
        }
    )
    decision_cfg = decision_config_from_app(cfg)
    assert decision_cfg.proxy_minimum_edge == cfg.strategy.min_edge
    assert decision_cfg.proxy_minimum_edge == 0.11
    assert decision_cfg.minimum_data_completeness == 0.375


def test_brti_history_roundtrip(tmp_path):
    path = tmp_path / "brti_history.json"
    now = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    points = [
        RollingPricePoint(timestamp=now, price=65000.0, source="BRTI", primary=True),
        RollingPricePoint(
            timestamp=datetime(2026, 8, 25, 11, 59, tzinfo=timezone.utc),
            price=64990.0,
            source="BRTI",
            primary=True,
        ),
    ]
    save_history(points, path)
    loaded = load_history(path)
    assert len(loaded) == 2
    assert loaded[-1].price == 65000.0
