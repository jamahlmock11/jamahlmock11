"""Tests for time-tiered net edge floors."""

import pytest

from kalshi_bot.config import DynamicEdgeBand, StrategyConfig
from kalshi_bot.strategies.edge_floor import (
    required_edge_from_bands,
    strategy_minimum_edge_floor,
    strategy_required_edge,
)


def _bands_15m() -> list[DynamicEdgeBand]:
    return [
        DynamicEdgeBand(min_minutes=10.0, max_minutes=15.0, min_edge=0.10),
        DynamicEdgeBand(min_minutes=7.0, max_minutes=10.0, min_edge=0.10),
        DynamicEdgeBand(min_minutes=5.0, max_minutes=7.0, min_edge=0.08),
        DynamicEdgeBand(min_minutes=3.0, max_minutes=5.0, min_edge=0.08),
        DynamicEdgeBand(min_minutes=0.0, max_minutes=3.0, min_edge=0.04),
    ]


def test_edge_tiers_by_time_remaining():
    bands = _bands_15m()
    assert required_edge_from_bands(840, bands, 0.10) == pytest.approx(0.10)  # 14m
    assert required_edge_from_bands(600, bands, 0.10) == pytest.approx(0.10)  # 10m
    assert required_edge_from_bands(420, bands, 0.10) == pytest.approx(0.08)  # 7m
    assert required_edge_from_bands(300, bands, 0.10) == pytest.approx(0.08)  # 5m
    assert required_edge_from_bands(120, bands, 0.10) == pytest.approx(0.04)  # 2m


def test_strategy_minimum_edge_floor_with_dynamic_bands():
    strategy = StrategyConfig(
        min_edge=0.10,
        dynamic_edge_enabled=True,
        dynamic_edge_bands=_bands_15m(),
    )
    assert strategy_minimum_edge_floor(strategy) == pytest.approx(0.04)


def test_strategy_required_edge_uses_dynamic_bands():
    strategy = StrategyConfig(
        min_edge=0.10,
        dynamic_edge_enabled=True,
        dynamic_edge_bands=_bands_15m(),
    )
    assert strategy_required_edge(120, strategy) == pytest.approx(0.04)
