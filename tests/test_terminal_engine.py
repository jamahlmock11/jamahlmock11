"""Tests for terminal probability, mispricing, and live 1h decision path."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.config import load_yaml_config, TerminalProbabilityConfig
from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    FeatureSnapshot,
    MarketSnapshot,
    Regime,
    TrajectoryState,
)
from kalshi_bot.hour.mispricing import assess_mispricing, required_edge_for_minutes
from kalshi_bot.hour.prediction_store import PredictionStore
from kalshi_bot.hour.terminal_decision import (
    HourTerminalDecisionEngine,
    terminal_decision_config_from_app,
)
from kalshi_bot.hour.terminal_probability import TerminalProbabilityEngine
from kalshi_bot.hour.trend_engine import classify_trend
from kalshi_bot.hour.volatility_model import analyze_volatility
from kalshi_bot.market.orderbook import parse_orderbook_fp

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _book(yes_ask: float = 0.50):
    yes_bid = yes_ask - 0.02
    no_bid = 1.0 - yes_ask
    return parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_bid:.4f}", "100"]],
                "no_dollars": [[f"{no_bid:.4f}", "100"]],
            }
        },
        timestamp=NOW,
    )


def hour_market(yes_ask: float = 0.50, *, minutes_remaining: float = 35.0, strike: float = 65_000):
    return MarketSnapshot(
        ticker="KXBTCD-26AUG081200-T65000",
        status="active",
        rules="60 second average of CF Benchmarks BRTI",
        strike=strike,
        expiration=NOW + timedelta(minutes=minutes_remaining),
        open_time=NOW - timedelta(minutes=60 - minutes_remaining),
        reference="CME CF Bitcoin Real Time Index (BRTI)",
        orderbook=_book(yes_ask),
    )


def _vol(features: FeatureSnapshot):
    return analyze_volatility(
        current_price=features.current_price,
        strike=features.strike,
        seconds_remaining=features.seconds_remaining,
        realized_vol=features.realized_vol,
        changes=dict(features.changes),
        prices=[65000, 65010, 65020],
        timestamps_span=3600,
    )


def hour_features(strike: float = 65_000, seconds_remaining: float = 2100):
    changes = {5: 0.0002, 15: 0.0003, 60: 0.0005, 300: 0.0007, 600: 0.0008, 1800: 0.001}
    return FeatureSnapshot(
        timestamp=NOW,
        current_price=65_020,
        strike=strike,
        seconds_remaining=seconds_remaining,
        changes=changes,
        velocities={60: 0.00004},
        acceleration=0.000001,
        short_trend=0.0003,
        medium_trend=0.0006,
        realized_vol=0.55,
        expected_remaining_move=200,
        z_distance_to_strike=0.1,
        mean_reversion_score=-0.1,
        orderbook_imbalance=0.1,
        cross_venue_agreement=0.85,
        cross_venue_dispersion=0.0002,
        data_completeness=0.9,
        trajectory=TrajectoryState.ACCELERATING_UP,
        sample_count=500,
        oldest_sample_age=3600,
    )


def benchmark():
    return BenchmarkQuote(
        price=65_020,
        timestamp=NOW,
        source="CME CF Bitcoin Real Time Index (BRTI)",
        primary=True,
        is_live=True,
    )


def test_strike_must_match_market_not_assumed_spot():
    features = hour_features(strike=64_500)
    market = hour_market(strike=65_000)
    engine = TerminalProbabilityEngine()
    trend = classify_trend(dict(features.changes))
    vol = _vol(features)
    terminal = engine.estimate(
        features,
        Regime.TREND_UP,
        trend,
        vol,
        market_strike=market.strike,
    )
    assert terminal.strike == 65_000
    assert terminal.strike != features.current_price


def test_terminal_probability_above_strike_when_brti_above():
    features = hour_features()
    engine = TerminalProbabilityEngine()
    trend = classify_trend(dict(features.changes))
    vol = _vol(features)
    terminal = engine.estimate(
        features,
        Regime.TREND_UP,
        trend,
        vol,
        market_strike=65_000,
    )
    assert terminal.calibrated_p_yes > 0.5
    assert terminal.expected_terminal_brti > 0
    assert terminal.terminal_volatility > 0


def test_dynamic_edge_bands():
    cfg = load_yaml_config("tests/fixtures/1h_terminal.yaml").terminal_probability
    assert required_edge_for_minutes(50, cfg) == pytest.approx(0.12)
    assert required_edge_for_minutes(12, cfg) == pytest.approx(0.12)
    assert required_edge_for_minutes(3, cfg) == pytest.approx(0.12)


def test_one_hour_edge_scenarios():
    """User reference scenarios: model minus executable vs time-tiered floor."""
    app_cfg = load_yaml_config("tests/fixtures/1h_terminal.yaml")
    engine = HourTerminalDecisionEngine(terminal_decision_config_from_app(app_cfg))

    def decide_at(
        *,
        minutes_remaining: float,
        model_yes: float,
        yes_ask: float,
    ):
        features = hour_features(seconds_remaining=minutes_remaining * 60)
        trend = classify_trend(dict(features.changes))
        vol = _vol(features)
        terminal = replace(
            TerminalProbabilityEngine().estimate(
                features,
                Regime.TREND_UP,
                trend,
                vol,
                market_strike=65_000,
            ),
            calibrated_p_yes=model_yes,
            calibrated_p_no=1.0 - model_yes,
            confidence=0.75,
            signal_agreement=0.70,
        )
        market = hour_market(yes_ask=yes_ask, minutes_remaining=minutes_remaining)
        return engine.decide(
            market,
            terminal,
            features,
            benchmark(),
            now=NOW,
            calibration_pass=True,
        )

  # 50m: outside last-15m entry window — NO TRADE
    decision, mispricing, _ = decide_at(minutes_remaining=50, model_yes=0.68, yes_ask=0.54)
    assert decision.action is DecisionAction.NO_TRADE
    assert any(f.gate == "time_window" for f in decision.gate_failures)

    # 12m: 88% vs 76% = 12¢ edge — trade (favorite band off)
    decision, _, _ = decide_at(minutes_remaining=12, model_yes=0.88, yes_ask=0.76)
    assert decision.action is DecisionAction.BUY_UP
    assert decision.edge == pytest.approx(0.12, abs=0.01)

    # 3m: 94% vs 82% = 12¢ edge — trade
    decision, _, _ = decide_at(minutes_remaining=3, model_yes=0.94, yes_ask=0.82)
    assert decision.action is DecisionAction.BUY_UP
    assert decision.edge == pytest.approx(0.12, abs=0.01)

    # 3m: 91% vs 88% = 3¢ edge — NO TRADE (below 12¢ floor)
    decision, _, _ = decide_at(minutes_remaining=3, model_yes=0.91, yes_ask=0.88)
    assert decision.action is DecisionAction.NO_TRADE
    assert any(f.gate == "minimum_edge" for f in decision.gate_failures)


def test_mispricing_buys_yes_when_underpriced():
    features = hour_features()
    engine = TerminalProbabilityEngine()
    trend = classify_trend(dict(features.changes))
    vol = _vol(features)
    terminal = engine.estimate(
        features,
        Regime.TREND_UP,
        trend,
        vol,
        market_strike=65_000,
    )
    # Force strong YES mispricing for test
    terminal = replace(
        terminal,
        calibrated_p_yes=0.72,
        calibrated_p_no=0.28,
        confidence=0.75,
        signal_agreement=0.70,
    )
    market = hour_market(yes_ask=0.50, minutes_remaining=35)
    cfg = load_yaml_config("tests/fixtures/1h_terminal.yaml").terminal_probability
    mispricing = assess_mispricing(
        terminal,
        market.orderbook,
        quantity=1,
        cfg=cfg,
    )
    assert mispricing.yes is not None
    assert mispricing.yes.net_edge > 0.15
    assert mispricing.best_side is ContractSide.YES


def test_no_trade_when_edge_too_small():
    features = hour_features()
    engine = TerminalProbabilityEngine()
    trend = classify_trend(dict(features.changes))
    vol = _vol(features)
    terminal = engine.estimate(
        features,
        Regime.TREND_UP,
        trend,
        vol,
        market_strike=65_000,
    )
    terminal = replace(
        terminal,
        calibrated_p_yes=0.62,
        calibrated_p_no=0.38,
        confidence=0.75,
        signal_agreement=0.70,
    )
    market = hour_market(yes_ask=0.58, minutes_remaining=35)
    app_cfg = load_yaml_config("tests/fixtures/1h_terminal.yaml")
    decision_engine = HourTerminalDecisionEngine(
        terminal_decision_config_from_app(app_cfg)
    )
    decision, _, _ = decision_engine.decide(
        market,
        terminal,
        features,
        benchmark(),
        now=NOW,
        calibration_pass=True,
    )
    assert decision.action is DecisionAction.NO_TRADE


def test_forecast_only_entry_when_mispricing_disabled():
    features = hour_features(seconds_remaining=12 * 60)
    trend = classify_trend(dict(features.changes))
    vol = _vol(features)
    terminal = replace(
        TerminalProbabilityEngine().estimate(
            features,
            Regime.TREND_UP,
            trend,
            vol,
            market_strike=65_000,
        ),
        calibrated_p_yes=0.88,
        calibrated_p_no=0.12,
        confidence=0.75,
        signal_agreement=0.70,
    )
    market = hour_market(yes_ask=0.76, minutes_remaining=12)
    app_cfg = load_yaml_config("tests/fixtures/1h_terminal.yaml")
    app_cfg = app_cfg.model_copy(
        update={
            "terminal_probability": app_cfg.terminal_probability.model_copy(
                update={"mispricing_enabled": False}
            )
        }
    )
    decision_engine = HourTerminalDecisionEngine(
        terminal_decision_config_from_app(app_cfg)
    )
    decision, mispricing, _ = decision_engine.decide(
        market,
        terminal,
        features,
        benchmark(),
        now=NOW,
        calibration_pass=True,
    )
    assert mispricing is not None
    assert mispricing.yes is not None
    assert abs(mispricing.yes.raw_edge - 0.12) < 0.011
    assert decision.action is DecisionAction.BUY_UP
    assert "tier" in decision.reason


def test_dynamic_edge_enforced_when_mispricing_disabled_1h():
    features = hour_features(seconds_remaining=3 * 60)
    trend = classify_trend(dict(features.changes))
    vol = _vol(features)
    terminal = replace(
        TerminalProbabilityEngine().estimate(
            features,
            Regime.TREND_UP,
            trend,
            vol,
            market_strike=65_000,
        ),
        calibrated_p_yes=0.91,
        calibrated_p_no=0.09,
        confidence=0.75,
        signal_agreement=0.70,
    )
    market = hour_market(yes_ask=0.88, minutes_remaining=3)
    app_cfg = load_yaml_config("tests/fixtures/1h_terminal.yaml")
    decision_engine = HourTerminalDecisionEngine(
        terminal_decision_config_from_app(app_cfg)
    )
    decision, _, _ = decision_engine.decide(
        market,
        terminal,
        features,
        benchmark(),
        now=NOW,
        calibration_pass=True,
    )
    assert decision.action is DecisionAction.NO_TRADE
    assert any(f.gate == "minimum_edge" for f in decision.gate_failures)


def test_terminal_decision_picks_best_side_not_direction_only():
    features = hour_features()
    trend = classify_trend(dict(features.changes))
    vol = _vol(features)
    engine = TerminalProbabilityEngine()
    terminal = engine.estimate(
        features,
        Regime.TREND_UP,
        trend,
        vol,
        market_strike=65_000,
    )
    terminal = replace(
        terminal,
        calibrated_p_yes=0.56,
        calibrated_p_no=0.44,
        confidence=0.75,
        signal_agreement=0.70,
    )
    market = hour_market(yes_ask=0.55, minutes_remaining=35)
    app_cfg = load_yaml_config("tests/fixtures/1h_terminal.yaml")
    decision_engine = HourTerminalDecisionEngine(
        terminal_decision_config_from_app(app_cfg)
    )
    decision, mispricing, _ = decision_engine.decide(
        market,
        terminal,
        features,
        benchmark(),
        now=NOW,
        calibration_pass=True,
    )
    if mispricing.best_side is ContractSide.NO and mispricing.no_net_edge >= mispricing.required_edge:
        assert decision.selected_side is ContractSide.NO or decision.action is DecisionAction.NO_TRADE


def test_prediction_store_records_and_resolves(tmp_path):
    store = PredictionStore(tmp_path / "pred.db")
    exp = NOW + timedelta(minutes=5)
    store.record(
        timestamp=NOW,
        ticker="KXBTCD-TEST",
        strike=65_000,
        expiration=exp,
        brti_price=65_020,
        seconds_remaining=300,
        predicted_p_yes=0.65,
        calibrated_p_yes=0.65,
        market_yes_ask=0.50,
        market_no_ask=0.50,
        yes_net_edge=0.12,
        no_net_edge=-0.05,
        volatility=0.5,
        regime="TREND_UP",
        confidence=0.7,
        signal_agreement=0.65,
        action="NO_TRADE",
    )
    resolved = store.resolve_expired(
        now=exp + timedelta(seconds=30),
        settlement_brti=65_100,
        ticker="KXBTCD-TEST",
    )
    assert resolved == 1
    calibrator = store.build_calibrator(cutoff=exp + timedelta(minutes=1))
    assert calibrator.fitted_sample_count >= 1


def test_1h_yaml_terminal_config_loaded():
    cfg = load_yaml_config("config/1h.yaml")
    assert cfg.terminal_probability.enabled is False
    assert cfg.terminal_probability.mispricing_enabled is False
    assert cfg.terminal_probability.dynamic_edge_enabled is False
    assert cfg.execution.orders_enabled is False
    assert cfg.execution.dry_run is True
    assert cfg.intelligence.enabled is False
    assert cfg.poll.mode == "disabled"
    assert cfg.longshot.enabled is False
    assert cfg.orderbook_skew.ensemble_enabled is False
    assert cfg.risk.position_reversal_enabled is False


def test_1h_terminal_fixture_config_loaded():
    cfg = load_yaml_config("tests/fixtures/1h_terminal.yaml")
    assert cfg.terminal_probability.enabled is True
    assert cfg.terminal_probability.intelligence_overlay is False
    assert cfg.terminal_probability.minimum_confidence == pytest.approx(0.52)
    assert cfg.terminal_probability.mispricing_enabled is True
    assert cfg.terminal_probability.exclude_coin_flip_band is True
    assert cfg.terminal_probability.exclude_longshot_band is True
    assert cfg.orderbook_skew.ensemble_enabled is True
    assert cfg.intelligence.enabled is False
    assert cfg.poll.mode == "disabled"


def test_coin_flip_band_blocks_near_fifty_cent_entries():
    features = hour_features()
    trend = classify_trend(dict(features.changes))
    vol = _vol(features)
    terminal = replace(
        TerminalProbabilityEngine().estimate(
            features,
            Regime.TREND_UP,
            trend,
            vol,
            market_strike=65_000,
        ),
        calibrated_p_yes=0.72,
        calibrated_p_no=0.28,
        confidence=0.75,
        signal_agreement=0.70,
    )
    market = hour_market(yes_ask=0.50, minutes_remaining=35)
    app_cfg = load_yaml_config("tests/fixtures/1h_terminal.yaml")
    decision_engine = HourTerminalDecisionEngine(
        terminal_decision_config_from_app(app_cfg)
    )
    decision, _, _ = decision_engine.decide(
        market,
        terminal,
        features,
        benchmark(),
        now=NOW,
        calibration_pass=True,
    )
    assert decision.action is DecisionAction.NO_TRADE
    assert any(f.gate == "coin_flip_band" for f in decision.gate_failures)


def test_favorite_band_allows_sixty_to_eighty_cent_entries():
    features = hour_features()
    trend = classify_trend(dict(features.changes))
    vol = _vol(features)
    terminal = replace(
        TerminalProbabilityEngine().estimate(
            features,
            Regime.TREND_UP,
            trend,
            vol,
            market_strike=65_000,
        ),
        calibrated_p_yes=0.78,
        calibrated_p_no=0.22,
        confidence=0.75,
        signal_agreement=0.70,
    )
    market = hour_market(yes_ask=0.68, minutes_remaining=35)
    app_cfg = load_yaml_config("tests/fixtures/1h_terminal.yaml")
    decision_engine = HourTerminalDecisionEngine(
        terminal_decision_config_from_app(app_cfg)
    )
    decision, mispricing, _ = decision_engine.decide(
        market,
        terminal,
        features,
        benchmark(),
        now=NOW,
        calibration_pass=True,
    )
    assert mispricing is not None
    assert mispricing.best_side is ContractSide.YES
    assert not any(f.gate == "coin_flip_band" for f in decision.gate_failures)
    assert not any(f.gate == "longshot_band" for f in decision.gate_failures)


def test_no_strike_blocks_trade():
    features = hour_features()
    market = hour_market()
    market = MarketSnapshot(
        ticker=market.ticker,
        status=market.status,
        rules=market.rules,
        strike=0,
        expiration=market.expiration,
        open_time=market.open_time,
        reference=market.reference,
        orderbook=market.orderbook,
    )
    engine = TerminalProbabilityEngine()
    trend = classify_trend(dict(features.changes))
    vol = _vol(features)
    terminal = engine.estimate(
        features,
        Regime.TREND_UP,
        trend,
        vol,
        market_strike=65_000,
    )
    app_cfg = load_yaml_config("tests/fixtures/1h_terminal.yaml")
    decision_engine = HourTerminalDecisionEngine(
        terminal_decision_config_from_app(app_cfg)
    )
    decision, _, _ = decision_engine.decide(
        market,
        terminal,
        features,
        benchmark(),
        now=NOW,
    )
    assert decision.action is DecisionAction.NO_TRADE
    assert any(f.gate == "contract_strike" for f in decision.gate_failures)
