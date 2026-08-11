from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    DecisionResult,
    Direction,
    FeatureSnapshot,
    ProbabilityEstimate,
    Regime,
    TrajectoryState,
)
from kalshi_bot.models.ensemble import EnsembleProbabilityModel
from kalshi_bot.strategies.decision import DecisionConfig, DecisionEngine
from kalshi_bot.strategies.entry_filters import (
    EntrySignalTracker,
    WindowRegimeKind,
    apply_signal_persistence_gate,
    classify_window_regime,
    is_in_chop_zone,
)
from tests.test_forecasting_core import benchmark, features, forecast, market

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _features(
    *,
    short_trend: float = 0.0003,
    medium_trend: float = 0.0006,
    z_distance: float = 0.25,
    realized_vol: float = 0.65,
) -> FeatureSnapshot:
    return replace(
        features(),
        short_trend=short_trend,
        medium_trend=medium_trend,
        z_distance_to_strike=z_distance,
        realized_vol=realized_vol,
        changes={5: 0.0002, 300: 0.0005, 600: 0.0006},
    )


def test_chop_zone_blocks_near_strike_entries():
    assert is_in_chop_zone(_features(z_distance=0.20), 0.35)
    assert not is_in_chop_zone(_features(z_distance=0.50), 0.35)


def test_classify_window_regime_detects_chop_from_conflicting_trends():
    regime = classify_window_regime(
        _features(short_trend=0.001, medium_trend=-0.001, realized_vol=0.70)
    )
    assert regime is WindowRegimeKind.CHOPPY


def test_classify_window_regime_detects_trending_alignment():
    regime = classify_window_regime(
        _features(short_trend=0.001, medium_trend=0.0008, realized_vol=0.55)
    )
    assert regime is WindowRegimeKind.TRENDING


def test_signal_persistence_requires_consecutive_polls():
    tracker = EntrySignalTracker(required_polls=3)
    decision = DecisionResult(
        action=DecisionAction.BUY_UP,
        reason="edge met",
        gate_failures=(),
        current_direction=Direction.FLAT,
        predicted_direction=Direction.UP,
        trade_direction=Direction.UP,
        selected_side=ContractSide.YES,
        edge=0.24,
        required_edge=0.20,
    )
    for _ in range(2):
        blocked = apply_signal_persistence_gate(
            decision, ticker="KXBTC15M-TEST", tracker=tracker
        )
        assert blocked.action is DecisionAction.NO_TRADE
    allowed = apply_signal_persistence_gate(
        decision, ticker="KXBTC15M-TEST", tracker=tracker
    )
    assert allowed.action is DecisionAction.BUY_UP


def test_decision_engine_applies_chop_zone_gate():
    engine = DecisionEngine(
        DecisionConfig(
            chop_zone_min_sigma=0.35,
            minimum_agreement=0.48,
            allow_proxy_data=False,
        )
    )
    result = engine.decide(
        market(),
        forecast(0.72),
        replace(features(), z_distance_to_strike=0.10),
        benchmark(),
    )
    assert result.action is DecisionAction.NO_TRADE
    assert any(failure.gate == "chop_zone" for failure in result.gate_failures)


def test_decision_engine_skips_depth_gate_when_disabled():
    engine = DecisionEngine(
        DecisionConfig(
            require_orderbook_depth=False,
            minimum_agreement=0.48,
            chop_zone_min_sigma=0.0,
            allow_proxy_data=False,
        )
    )
    result = engine.decide(
        market(),
        forecast(0.72),
        features(),
        benchmark(),
    )
    assert not any(
        failure.gate.endswith("_liquidity") for failure in result.gate_failures
    )


def test_ensemble_downweights_momentum_in_choppy_window():
    model = EnsembleProbabilityModel()
    base = model.estimate(
        _features(short_trend=0.001, medium_trend=0.0008),
        Regime.TREND_UP,
        window_regime=WindowRegimeKind.TRENDING,
    )
    choppy = model.estimate(
        _features(short_trend=0.001, medium_trend=-0.001, realized_vol=0.75),
        Regime.TREND_UP,
        window_regime=WindowRegimeKind.CHOPPY,
    )
    assert choppy.confidence <= base.confidence
