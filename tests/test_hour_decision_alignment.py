"""Tests for 1-hour reversal config alignment."""

from __future__ import annotations

import pytest

from kalshi_bot.config import load_yaml_config


def test_1h_config_uses_reversal_strategy_only():
    cfg = load_yaml_config("config/1h.yaml")
    assert cfg.horizon == "1h"
    assert cfg.hour_reversal.enabled is True
    assert cfg.hour_reversal.min_reversal_score == pytest.approx(70)
    assert cfg.hour_reversal.min_entry_edge == pytest.approx(0.15)
    assert cfg.longshot.enabled is False
    assert cfg.intelligence.enabled is False
    assert cfg.poll.mode == "disabled"
