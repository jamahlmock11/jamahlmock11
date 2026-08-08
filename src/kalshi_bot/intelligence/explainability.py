"""Post-trade explainability reports."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.domain import DecisionAction, DecisionResult, FeatureSnapshot, ProbabilityEstimate, Regime
from kalshi_bot.intelligence.institutional_flow import FlowAssessment
from kalshi_bot.intelligence.manipulation import ManipulationAssessment
from kalshi_bot.intelligence.signals import TechnicalSignals
from kalshi_bot.models.strike_gravity import StrikeGravityAssessment
from kalshi_bot.models.trading_regime import TradingRegime


@dataclass(frozen=True)
class ExplainabilityReport:
    decision: str
    reasons: tuple[str, ...]
    confidence: float
    expected_value_pct: float
    monte_carlo_up: float | None
    monte_carlo_down: float | None
    regime: str
    manipulation_detected: bool
    kill_switch_active: bool

    def format_report(self) -> str:
        lines = [
            f"Decision: {self.decision}",
            "",
            "Reason:",
        ]
        for reason in self.reasons:
            lines.append(reason)
        lines.extend(
            [
                "",
                f"Confidence: {self.confidence:.0%}",
                "",
                f"Expected Value: {self.expected_value_pct:+.0%}",
            ]
        )
        if self.monte_carlo_up is not None:
            lines.append(f"Monte Carlo: UP {self.monte_carlo_up:.1%} · DOWN {self.monte_carlo_down:.1%}")
        lines.append(f"Market Regime: {self.regime}")
        if not self.manipulation_detected:
            lines.append("No spoofing detected")
        return "\n".join(lines)


def build_explainability(
    decision: DecisionResult,
    forecast: ProbabilityEstimate,
    features: FeatureSnapshot,
    signals: TechnicalSignals,
    trading_regime: TradingRegime,
    manipulation: ManipulationAssessment,
    flow: FlowAssessment,
    strike_gravity: StrikeGravityAssessment,
    *,
    monte_carlo_up: float | None = None,
    monte_carlo_down: float | None = None,
    kill_switch_active: bool = False,
    signal_accuracies: dict[str, float] | None = None,
) -> ExplainabilityReport:
    """Build a human-readable explainability report after each decision."""
    action_label = {
        DecisionAction.BUY_UP: "BUY UP",
        DecisionAction.BUY_DOWN: "BUY DOWN",
        DecisionAction.HOLD: "HOLD",
        DecisionAction.EXIT: "EXIT",
        DecisionAction.NO_TRADE: "NO TRADE",
    }.get(decision.action, decision.action.value)

    reasons: list[str] = []
    if signals.ema_bearish:
        reasons.append("EMA Bearish")
    else:
        reasons.append("EMA Bullish")
    reasons.append(f"Orderbook {signals.orderbook:.0%} {'Buy' if signals.orderbook >= 0.5 else 'Sell'}")
    distance = features.current_price - features.strike
    reasons.append(f"BTC ${abs(distance):,.0f} {'above' if distance >= 0 else 'below'} strike")
    if features.realized_vol > 0.65:
        reasons.append("Volatility Rising")
    elif features.realized_vol < 0.35:
        reasons.append("Volatility Low")

    if signal_accuracies:
        best_signal = max(signal_accuracies, key=signal_accuracies.get)
        reasons.append(f"Historical {signal_accuracies[best_signal]:.0%} ({best_signal})")

    if monte_carlo_up is not None:
        mc_side = monte_carlo_up if decision.action == DecisionAction.BUY_UP else monte_carlo_down
        if mc_side is not None:
            reasons.append(f"Monte Carlo {mc_side:.0%}")

    reasons.append(f"Market Regime: {trading_regime.label}")
    if strike_gravity.finish_probability_up >= 0.55:
        reasons.append(f"Strike gravity favors UP ({strike_gravity.finish_probability_up:.0%})")
    elif strike_gravity.finish_probability_up <= 0.45:
        reasons.append(f"Strike gravity favors DOWN ({1 - strike_gravity.finish_probability_up:.0%})")

    for flow_reason in flow.reasons[:2]:
        if flow_reason != "no institutional flow signals":
            reasons.append(flow_reason)

    if manipulation.detected:
        for manip_reason in manipulation.reasons:
            if manip_reason != "no spoofing detected":
                reasons.append(f"Manipulation warning: {manip_reason}")
    else:
        reasons.append("No spoofing detected")

    edge = decision.edge or 0.0
    ev_pct = edge * 100 if decision.edge is not None else 0.0

    return ExplainabilityReport(
        decision=action_label,
        reasons=tuple(reasons),
        confidence=forecast.confidence,
        expected_value_pct=ev_pct,
        monte_carlo_up=monte_carlo_up,
        monte_carlo_down=monte_carlo_down,
        regime=trading_regime.label,
        manipulation_detected=manipulation.detected,
        kill_switch_active=kill_switch_active,
    )
