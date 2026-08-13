"""Weighted setup score for forecast entries (momentum, structure, flow, time)."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.domain import FeatureSnapshot, Regime
from kalshi_bot.features.enriched import EnrichedFeatures
from kalshi_bot.strategies.reversal_score import (
    ReversalScoreComponents,
    _clip,
    _compute_setup_component_values,
    _initial_direction,
)


FORECAST_SETUP_WEIGHTS: dict[str, float] = {
    "momentum_exhaustion": 23.0,
    "structure_break": 19.0,
    "volume_confirmation": 15.0,
    "order_flow_reversal": 15.0,
    "volatility_shift": 10.0,
    "distance_from_strike": 9.0,
    "time_remaining": 7.0,
}


@dataclass(frozen=True)
class ForecastSetupAssessment:
    """0–100 weighted setup score blended into forecast trade quality."""

    score: float
    components: ReversalScoreComponents
    initial_direction: str
    in_sweet_spot: bool
    summary: str


def compute_forecast_setup_score(
    features: FeatureSnapshot,
    enriched: EnrichedFeatures,
    regime: Regime,
    *,
    seconds_remaining: float,
    sweet_spot_min_seconds: float = 180.0,
    sweet_spot_max_seconds: float = 600.0,
    min_initial_move_z: float = 0.50,
) -> ForecastSetupAssessment:
    """Score path/momentum/flow setup for forecast ensemble entries."""
    initial = _initial_direction(features)
    comp = _compute_setup_component_values(
        features,
        enriched,
        regime,
        seconds_remaining=seconds_remaining,
        sweet_spot_min_seconds=sweet_spot_min_seconds,
        sweet_spot_max_seconds=sweet_spot_max_seconds,
        min_initial_move_z=min_initial_move_z,
    )
    raw = sum(
        getattr(comp, key) * weight for key, weight in FORECAST_SETUP_WEIGHTS.items()
    )
    score = round(_clip(raw, 0.0, 100.0), 1)
    minutes = seconds_remaining / 60.0
    in_sweet_spot = (
        sweet_spot_min_seconds / 60.0 <= minutes <= sweet_spot_max_seconds / 60.0
    )
    summary = (
        f"SETUP {score:.0f}/100 · {initial} path · "
        f"mom {comp.momentum_exhaustion:.0%} struct {comp.structure_break:.0%} · "
        f"time {'sweet' if in_sweet_spot else 'off-spot'}"
    )
    return ForecastSetupAssessment(
        score=score,
        components=comp,
        initial_direction=initial,
        in_sweet_spot=in_sweet_spot,
        summary=summary,
    )
