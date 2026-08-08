"""Read-only analysis of decision journals and forecast behavior."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from kalshi_bot.backtest.performance import calculate_performance, time_to_expiry_bucket


JournalDecision = Mapping[str, object] | object


@dataclass(frozen=True)
class SegmentAnalysis:
    """Observed outcomes for one journal segment."""

    decision_count: int
    resolved_count: int
    trade_count: int
    accuracy: float
    average_pnl: float
    average_predicted_edge: float
    brier_score: float
    calibration_error: float
    reversal_correctness: float
    breakout_correctness: float


@dataclass(frozen=True)
class Recommendation:
    """An observational hypothesis, never a live weight mutation."""

    segment: str
    observation: str
    recommendation: str
    requires_walk_forward_validation: bool = True
    mutates_live_weights: bool = False


@dataclass(frozen=True)
class AnalysisReport:
    """Overall and sliced decision-journal analysis."""

    overall: SegmentAnalysis
    by_signal: Mapping[str, SegmentAnalysis]
    by_regime: Mapping[str, SegmentAnalysis]
    by_trajectory: Mapping[str, SegmentAnalysis]
    by_time_bucket: Mapping[str, SegmentAnalysis]
    recommendations: tuple[Recommendation, ...]


def _value(record: JournalDecision, name: str, default: object = None) -> object:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _label(record: JournalDecision, name: str, default: str = "UNKNOWN") -> str:
    value = _value(record, name)
    if value is None:
        return default
    return str(getattr(value, "value", value))


def _number(record: JournalDecision, name: str) -> float | None:
    value = _value(record, name)
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _correct(record: JournalDecision) -> bool | None:
    explicit = _value(record, "correct")
    if isinstance(explicit, bool):
        return explicit
    probability = _number(record, "predicted_probability")
    outcome = _number(record, "outcome")
    if probability is None or outcome not in (0.0, 1.0):
        return None
    if _value(record, "selected_side") is not None:
        return bool(outcome)
    return (probability >= 0.5) == bool(outcome)


def _is_trade(record: JournalDecision) -> bool:
    action = _label(record, "action", "")
    return action in {"BUY_UP", "BUY_DOWN", "ENTRY", "EXIT", "SETTLEMENT"}


def _special_correctness(
    records: list[JournalDecision],
    *,
    kind: str,
) -> float:
    if kind == "reversal":
        selected = [
            record
            for record in records
            if _label(record, "regime").startswith("REVERSAL")
            or _label(record, "trajectory").startswith("REVERSING")
        ]
    else:
        selected = [
            record
            for record in records
            if _label(record, "regime") in {"BREAKOUT", "BREAKDOWN"}
            or "BREAKOUT" in _label(record, "signal")
        ]
    values = [result for record in selected if (result := _correct(record)) is not None]
    return sum(values) / len(values) if values else 0.0


def _segment(records: list[JournalDecision]) -> SegmentAnalysis:
    correctness = [result for record in records if (result := _correct(record)) is not None]
    pnls = [value for record in records if (value := _number(record, "pnl")) is not None]
    edges = [
        value
        for record in records
        if (value := _number(record, "predicted_edge")) is not None
    ]
    performance = calculate_performance((), records).overall
    return SegmentAnalysis(
        decision_count=len(records),
        resolved_count=len(correctness),
        trade_count=sum(_is_trade(record) for record in records),
        accuracy=sum(correctness) / len(correctness) if correctness else 0.0,
        average_pnl=sum(pnls) / len(pnls) if pnls else 0.0,
        average_predicted_edge=sum(edges) / len(edges) if edges else 0.0,
        brier_score=performance.brier_score,
        calibration_error=performance.calibration_error,
        reversal_correctness=_special_correctness(records, kind="reversal"),
        breakout_correctness=_special_correctness(records, kind="breakout"),
    )


def _group(
    records: list[JournalDecision],
    key: str,
) -> dict[str, SegmentAnalysis]:
    labels = sorted({_label(record, key) for record in records})
    return {
        label: _segment([record for record in records if _label(record, key) == label])
        for label in labels
    }


class LearningAnalyzer:
    """Generate observations from journals without touching model weights."""

    def __init__(self, *, minimum_recommendation_samples: int = 20) -> None:
        if minimum_recommendation_samples <= 0:
            raise ValueError("minimum_recommendation_samples must be positive")
        self.minimum_recommendation_samples = minimum_recommendation_samples

    def analyze(self, decisions: Iterable[JournalDecision]) -> AnalysisReport:
        """Analyze resolved and unresolved decisions across causal segments."""
        records = list(decisions)
        by_signal = _group(records, "signal")
        by_regime = _group(records, "regime")
        by_trajectory = _group(records, "trajectory")

        def time_label(record: JournalDecision) -> str:
            explicit = _value(record, "time_to_expiry_bucket")
            if explicit is not None:
                return str(explicit)
            return time_to_expiry_bucket(_number(record, "time_to_expiry"))

        time_labels = sorted({time_label(record) for record in records})
        by_time = {
            label: _segment([record for record in records if time_label(record) == label])
            for label in time_labels
        }
        recommendations = self._recommendations(
            {
                **{f"signal:{key}": value for key, value in by_signal.items()},
                **{f"regime:{key}": value for key, value in by_regime.items()},
                **{f"trajectory:{key}": value for key, value in by_trajectory.items()},
                **{f"time:{key}": value for key, value in by_time.items()},
            }
        )
        return AnalysisReport(
            overall=_segment(records),
            by_signal=by_signal,
            by_regime=by_regime,
            by_trajectory=by_trajectory,
            by_time_bucket=by_time,
            recommendations=recommendations,
        )

    def _recommendations(
        self,
        segments: Mapping[str, SegmentAnalysis],
    ) -> tuple[Recommendation, ...]:
        result: list[Recommendation] = []
        for name, segment in sorted(segments.items()):
            if segment.resolved_count < self.minimum_recommendation_samples:
                continue
            if segment.calibration_error >= 0.10:
                result.append(
                    Recommendation(
                        segment=name,
                        observation=(
                            f"observed calibration error is "
                            f"{segment.calibration_error:.3f} over "
                            f"{segment.resolved_count} resolved decisions"
                        ),
                        recommendation=(
                            "evaluate segment-specific calibration in an isolated "
                            "walk-forward experiment before changing production"
                        ),
                    )
                )
            if segment.accuracy < 0.50:
                result.append(
                    Recommendation(
                        segment=name,
                        observation=(
                            f"observed directional accuracy is {segment.accuracy:.3f} "
                            f"over {segment.resolved_count} resolved decisions"
                        ),
                        recommendation=(
                            "test reduced reliance on this segment out of sample; "
                            "do not alter live ensemble weights without walk-forward validation"
                        ),
                    )
                )
        return tuple(result)


def analyze_journal(
    decisions: Iterable[JournalDecision],
    *,
    minimum_recommendation_samples: int = 20,
) -> AnalysisReport:
    """Convenience wrapper for read-only journal analysis."""
    return LearningAnalyzer(
        minimum_recommendation_samples=minimum_recommendation_samples
    ).analyze(decisions)
