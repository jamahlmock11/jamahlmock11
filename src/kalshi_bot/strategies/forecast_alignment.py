"""Dynamic forecast-alignment filter for contrarian mispricing entries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kalshi_bot.domain import ContractSide, GateFailure, ProbabilityEstimate

if TYPE_CHECKING:
    from kalshi_bot.config import ForecastAlignmentConfig


@dataclass
class ForecastAlignmentTracker:
    """Per-ticker prior model probability for stability / deterioration checks."""

    _prior_p_up: dict[str, float] = field(default_factory=dict)

    def prior_p_up(self, ticker: str) -> float | None:
        return self._prior_p_up.get(ticker)

    def record(self, ticker: str, p_up: float) -> None:
        self._prior_p_up[ticker] = p_up


@dataclass(frozen=True)
class ForecastAlignmentAssessment:
    enabled: bool
    conflict: bool
    aligned: bool
    model_probability: float
    kalshi_price: float
    edge: float
    base_required_edge: float
    effective_required_edge: float
    forecast_direction: str
    dominant_probability: float
    conflict_status: str
    probability_stable: bool
    probability_deteriorating: bool
    exceptional_edge: bool
    confirmation_met: bool
    final_decision: str
    reason: str

    def as_log_dict(self) -> dict[str, object]:
        return {
            "model_probability": self.model_probability,
            "kalshi_price": self.kalshi_price,
            "edge": self.edge,
            "forecast_direction": self.forecast_direction,
            "dominant_probability": self.dominant_probability,
            "conflict_status": self.conflict_status,
            "base_required_edge": self.base_required_edge,
            "effective_required_edge": self.effective_required_edge,
            "probability_stable": self.probability_stable,
            "probability_deteriorating": self.probability_deteriorating,
            "exceptional_edge": self.exceptional_edge,
            "confirmation_met": self.confirmation_met,
            "final_decision": self.final_decision,
            "reason": self.reason,
        }


def _direction_for_side(side: ContractSide) -> str:
    return "UP" if side is ContractSide.YES else "DOWN"


def _failure(gate: str, reason: str, observed: object, required: object) -> GateFailure:
    return GateFailure(gate=gate, reason=reason, observed=observed, required=required)


def evaluate_forecast_alignment(
    *,
    ticker: str,
    selected_side: ContractSide,
    side_probabilities: dict[ContractSide, float],
    forecast: ProbabilityEstimate,
    executable_cost: float,
    edge: float,
    required_edge: float,
    cfg: ForecastAlignmentConfig,
    tracker: ForecastAlignmentTracker | None = None,
) -> tuple[ForecastAlignmentAssessment, GateFailure | None]:
    """
    Apply dynamic forecast-alignment risk filtering for contrarian entries.

    Never hard-blocks aligned trades. On conflict, require stronger edge and
    confirmation unless probability is unstable/deteriorating (PASS).
    """
    model_probability = side_probabilities[selected_side]
    dominant_side = (
        ContractSide.YES if forecast.p_up >= forecast.p_down else ContractSide.NO
    )
    dominant_probability = max(forecast.p_up, forecast.p_down)
    forecast_direction = _direction_for_side(dominant_side)
    prior_p_up = tracker.prior_p_up(ticker) if tracker is not None else None

    if tracker is not None:
        tracker.record(ticker, forecast.p_up)

    if not cfg.enabled:
        assessment = ForecastAlignmentAssessment(
            enabled=False,
            conflict=False,
            aligned=True,
            model_probability=model_probability,
            kalshi_price=executable_cost,
            edge=edge,
            base_required_edge=required_edge,
            effective_required_edge=required_edge,
            forecast_direction=forecast_direction,
            dominant_probability=dominant_probability,
            conflict_status="disabled",
            probability_stable=True,
            probability_deteriorating=False,
            exceptional_edge=False,
            confirmation_met=True,
            final_decision="allow",
            reason="forecast alignment filter disabled",
        )
        return assessment, None

    conflict = (
        selected_side is not dominant_side
        and dominant_probability + 1e-12 >= cfg.dominant_min_probability
    )
    aligned = not conflict

    selected_change = 0.0
    if prior_p_up is not None:
        if selected_side is ContractSide.YES:
            selected_change = forecast.p_up - prior_p_up
        else:
            selected_change = (1.0 - forecast.p_up) - (1.0 - prior_p_up)

    probability_deteriorating = (
        prior_p_up is not None
        and selected_change + 1e-12 <= -cfg.max_probability_deterioration
    )
    probability_stable = (
        forecast.confidence + 1e-12 >= cfg.min_stability_confidence
        and forecast.signal_agreement + 1e-12 >= cfg.min_stability_agreement
        and not probability_deteriorating
    )
    exceptional_edge = edge + 1e-12 >= cfg.exceptional_edge_threshold
    confirmation_met = (
        forecast.confidence + 1e-12 >= cfg.min_conflict_confidence
        and forecast.signal_agreement + 1e-12 >= cfg.min_conflict_agreement
        and model_probability + 1e-12 >= cfg.min_selected_probability
    )
    effective_required_edge = required_edge

    if aligned:
        assessment = ForecastAlignmentAssessment(
            enabled=True,
            conflict=False,
            aligned=True,
            model_probability=model_probability,
            kalshi_price=executable_cost,
            edge=edge,
            base_required_edge=required_edge,
            effective_required_edge=required_edge,
            forecast_direction=forecast_direction,
            dominant_probability=dominant_probability,
            conflict_status="aligned",
            probability_stable=probability_stable,
            probability_deteriorating=probability_deteriorating,
            exceptional_edge=exceptional_edge,
            confirmation_met=True,
            final_decision="allow",
            reason="selected side aligns with dominant strike-expiry forecast",
        )
        return assessment, None

    conflict_status = "conflict"
    if not probability_stable:
        reason = (
            "contrarian setup rejected: probability unstable or deteriorating "
            f"(Δselected {selected_change:+.1%}, confidence {forecast.confidence:.1%}, "
            f"agreement {forecast.signal_agreement:.1%})"
        )
        assessment = ForecastAlignmentAssessment(
            enabled=True,
            conflict=True,
            aligned=False,
            model_probability=model_probability,
            kalshi_price=executable_cost,
            edge=edge,
            base_required_edge=required_edge,
            effective_required_edge=required_edge,
            forecast_direction=forecast_direction,
            dominant_probability=dominant_probability,
            conflict_status=conflict_status,
            probability_stable=False,
            probability_deteriorating=probability_deteriorating,
            exceptional_edge=exceptional_edge,
            confirmation_met=confirmation_met,
            final_decision="pass",
            reason=reason,
        )
        return assessment, _failure(
            "forecast_alignment",
            reason,
            selected_change if prior_p_up is not None else forecast.confidence,
            cfg.max_probability_deterioration,
        )

    if exceptional_edge:
        reason = (
            "contrarian mispricing allowed: exceptional edge "
            f"{edge:.1%} with stable selected-side probability {model_probability:.1%}"
        )
        assessment = ForecastAlignmentAssessment(
            enabled=True,
            conflict=True,
            aligned=False,
            model_probability=model_probability,
            kalshi_price=executable_cost,
            edge=edge,
            base_required_edge=required_edge,
            effective_required_edge=required_edge,
            forecast_direction=forecast_direction,
            dominant_probability=dominant_probability,
            conflict_status=conflict_status,
            probability_stable=True,
            probability_deteriorating=False,
            exceptional_edge=True,
            confirmation_met=confirmation_met,
            final_decision="allow",
            reason=reason,
        )
        return assessment, None

    effective_required_edge = required_edge + cfg.contrarian_edge_premium
    if edge + 1e-12 < effective_required_edge:
        reason = (
            "contrarian setup needs stronger mispricing: edge "
            f"{edge:.1%} below conflict-adjusted floor {effective_required_edge:.1%} "
            f"(base {required_edge:.1%} + premium {cfg.contrarian_edge_premium:.1%})"
        )
        assessment = ForecastAlignmentAssessment(
            enabled=True,
            conflict=True,
            aligned=False,
            model_probability=model_probability,
            kalshi_price=executable_cost,
            edge=edge,
            base_required_edge=required_edge,
            effective_required_edge=effective_required_edge,
            forecast_direction=forecast_direction,
            dominant_probability=dominant_probability,
            conflict_status=conflict_status,
            probability_stable=True,
            probability_deteriorating=False,
            exceptional_edge=False,
            confirmation_met=confirmation_met,
            final_decision="pass",
            reason=reason,
        )
        return assessment, _failure(
            "forecast_alignment",
            reason,
            edge,
            effective_required_edge,
        )

    if not confirmation_met:
        reason = (
            "contrarian setup lacks confirmation against dominant "
            f"{forecast_direction} forecast "
            f"(confidence {forecast.confidence:.1%}, agreement "
            f"{forecast.signal_agreement:.1%}, selected prob {model_probability:.1%})"
        )
        assessment = ForecastAlignmentAssessment(
            enabled=True,
            conflict=True,
            aligned=False,
            model_probability=model_probability,
            kalshi_price=executable_cost,
            edge=edge,
            base_required_edge=required_edge,
            effective_required_edge=effective_required_edge,
            forecast_direction=forecast_direction,
            dominant_probability=dominant_probability,
            conflict_status=conflict_status,
            probability_stable=True,
            probability_deteriorating=False,
            exceptional_edge=False,
            confirmation_met=False,
            final_decision="pass",
            reason=reason,
        )
        return assessment, _failure(
            "forecast_alignment",
            reason,
            model_probability,
            cfg.min_selected_probability,
        )

    reason = (
        "contrarian mispricing allowed: stable probability with elevated edge "
        f"{edge:.1%} vs conflict floor {effective_required_edge:.1%}"
    )
    assessment = ForecastAlignmentAssessment(
        enabled=True,
        conflict=True,
        aligned=False,
        model_probability=model_probability,
        kalshi_price=executable_cost,
        edge=edge,
        base_required_edge=required_edge,
        effective_required_edge=effective_required_edge,
        forecast_direction=forecast_direction,
        dominant_probability=dominant_probability,
        conflict_status=conflict_status,
        probability_stable=True,
        probability_deteriorating=False,
        exceptional_edge=False,
        confirmation_met=True,
        final_decision="allow",
        reason=reason,
    )
    return assessment, None
