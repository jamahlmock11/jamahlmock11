"""Orchestrate all intelligence modules into a unified assessment."""

from __future__ import annotations

from dataclasses import dataclass, replace

from kalshi_bot.calibration.calibration import ProbabilityCalibrator
from kalshi_bot.domain import FeatureSnapshot, MarketSnapshot, ProbabilityEstimate, Regime, SupportingAggregate
from kalshi_bot.intelligence.explainability import ExplainabilityReport, build_explainability
from kalshi_bot.intelligence.institutional_flow import FlowAssessment, InstitutionalFlowDetector
from kalshi_bot.intelligence.kill_switch import ConfidenceKillSwitch, KillSwitchState
from kalshi_bot.intelligence.manipulation import ManipulationAssessment, ManipulationDetector
from kalshi_bot.intelligence.signals import TechnicalSignals, compute_technical_signals
from kalshi_bot.learning.signal_weights import SignalWeightTracker
from kalshi_bot.models.monte_carlo import MonteCarloResult, simulate_finish_probability
from kalshi_bot.models.strike_gravity import StrikeGravityAssessment, assess_strike_gravity
from kalshi_bot.models.trading_regime import TradingRegime, blend_signal_probability, classify_trading_regime


@dataclass(frozen=True)
class IntelligenceReport:
    """Combined intelligence output for one forecast cycle."""

    signals: TechnicalSignals
    trading_regime: TradingRegime
    manipulation: ManipulationAssessment
    flow: FlowAssessment
    strike_gravity: StrikeGravityAssessment
    monte_carlo: MonteCarloResult
    kill_switch: KillSwitchState
    signal_blend_p_up: float
    adjusted_forecast: ProbabilityEstimate | None
    explainability: ExplainabilityReport | None
    skip_trade: bool
    skip_reason: str


class IntelligenceOrchestrator:
    """Apply information advantage, calibration, and risk intelligence to forecasts."""

    def __init__(
        self,
        *,
        calibrator: ProbabilityCalibrator | None = None,
        kill_switch: ConfidenceKillSwitch | None = None,
        manipulation_detector: ManipulationDetector | None = None,
        flow_detector: InstitutionalFlowDetector | None = None,
        signal_weights: SignalWeightTracker | None = None,
        monte_carlo_paths: int = 7500,
        confidence_threshold: float = 0.60,
    ) -> None:
        self.calibrator = calibrator
        self.kill_switch = kill_switch or ConfidenceKillSwitch()
        self.manipulation = manipulation_detector or ManipulationDetector()
        self.flow = flow_detector or InstitutionalFlowDetector()
        self.signal_weights = signal_weights or SignalWeightTracker()
        self.monte_carlo_paths = monte_carlo_paths
        self.confidence_threshold = confidence_threshold

    def enrich(
        self,
        forecast: ProbabilityEstimate,
        features: FeatureSnapshot,
        market: MarketSnapshot,
        regime: Regime,
        decision_action: str | None = None,
        decision_edge: float | None = None,
        supporting: SupportingAggregate | None = None,
    ) -> IntelligenceReport:
        """Run all intelligence modules and return adjusted assessment."""
        signals = compute_technical_signals(features, market.orderbook)
        trading_regime = classify_trading_regime(features, regime)
        learned_weights = self.signal_weights.apply_weights(trading_regime.signal_weights)
        signal_blend = blend_signal_probability(signals.as_probabilities(), learned_weights)

        manipulation = self.manipulation.assess(market.orderbook)
        flow = self.flow.assess(features, market.orderbook, supporting)
        strike_gravity = assess_strike_gravity(features)

        mc = simulate_finish_probability(
            features,
            paths=self.monte_carlo_paths,
            orderbook_bias=features.orderbook_imbalance,
            news_bias=flow.flow_direction * 0.1,
            seed=int(features.timestamp.timestamp()) % 1_000_000,
        )

        # Blend ensemble, signal blend, strike gravity, and Monte Carlo
        raw_blend = (
            0.35 * forecast.p_up
            + 0.20 * signal_blend
            + 0.20 * strike_gravity.finish_probability_up
            + 0.25 * mc.p_up
        )

        calibrated_p_up = raw_blend
        if self.calibrator is not None and self.calibrator.fit_cutoff is not None:
            calibrated_p_up = self.calibrator.transform(raw_blend)

        confidence = forecast.confidence
        if manipulation.detected:
            confidence = max(0.0, confidence - manipulation.confidence_penalty)
        if flow.confidence_boost > 0:
            confidence = min(1.0, confidence + flow.confidence_boost)

        skip_trade = False
        skip_reason = ""
        can_trade, kill_reason = self.kill_switch.should_trade()
        if not can_trade:
            skip_trade = True
            skip_reason = kill_reason
        elif confidence < self.confidence_threshold:
            skip_trade = True
            skip_reason = f"confidence {confidence:.1%} below threshold {self.confidence_threshold:.0%}"

        adjusted = replace(
            forecast,
            p_up=calibrated_p_up,
            p_down=1.0 - calibrated_p_up,
            confidence=confidence,
            raw_p_up=raw_blend,
            calibrated=self.calibrator is not None and self.calibrator.fit_cutoff is not None,
            notes=forecast.notes + (
                f"intelligence blend p_up={raw_blend:.4f}",
                f"monte_carlo up={mc.p_up:.4f}",
                f"regime={trading_regime.label}",
            ),
        )

        explainability = None
        if decision_action is not None:
            from kalshi_bot.domain import DecisionAction, DecisionResult, Direction

            action_map = {v.value: v for v in DecisionAction}
            action = action_map.get(decision_action, DecisionAction.NO_TRADE)
            stub_decision = DecisionResult(
                action=action,
                reason="",
                gate_failures=(),
                current_direction=Direction.FLAT,
                predicted_direction=Direction.UP if calibrated_p_up >= 0.5 else Direction.DOWN,
                trade_direction=Direction.FLAT,
                edge=decision_edge,
            )
            explainability = build_explainability(
                stub_decision,
                adjusted,
                features,
                signals,
                trading_regime,
                manipulation,
                flow,
                strike_gravity,
                monte_carlo_up=mc.p_up,
                monte_carlo_down=mc.p_down,
                kill_switch_active=self.kill_switch.halted,
                signal_accuracies=self.signal_weights.accuracies(),
            )

        return IntelligenceReport(
            signals=signals,
            trading_regime=trading_regime,
            manipulation=manipulation,
            flow=flow,
            strike_gravity=strike_gravity,
            monte_carlo=mc,
            kill_switch=self.kill_switch.check(),
            signal_blend_p_up=signal_blend,
            adjusted_forecast=adjusted,
            explainability=explainability,
            skip_trade=skip_trade,
            skip_reason=skip_reason,
        )
