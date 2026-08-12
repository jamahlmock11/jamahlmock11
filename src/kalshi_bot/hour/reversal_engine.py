"""Reversal score (0–100) for 1-hour Kalshi lag / momentum-failure setups."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from kalshi_bot.config import HourReversalConfig
from kalshi_bot.domain import (
    Direction,
    FeatureSnapshot,
    ProbabilityEstimate,
    SupportingAggregate,
    TrajectoryState,
)
from kalshi_bot.hour.reversal_state import ContractReversalState
from kalshi_bot.hour.trend_engine import TrendSnapshot
from kalshi_bot.hour.volatility_model import VolatilitySnapshot
from kalshi_bot.market.poll_alignment import PollSnapshot


class ReversalTier(str, Enum):
    NONE = "none"
    WATCH = "watch"
    CANDIDATE = "candidate"
    STRONG = "strong"


@dataclass(frozen=True)
class ReversalScoreComponents:
    momentum_exhaustion: float
    structure_break: float
    volume_confirmation: float
    order_flow_reversal: float
    cross_exchange_confirmation: float
    volatility_shift: float
    distance_from_strike: float
    time_remaining: float
    kalshi_repricing_lag: float
    model_probability_change: float


@dataclass(frozen=True)
class ReversalAssessment:
    score: float
    tier: ReversalTier
    components: ReversalScoreComponents
    initial_direction: Direction | None
    reversal_direction: Direction | None
    reversal_side_probability: float | None
    kalshi_lag_cents: float | None
    probability_drop: float | None
    summary: str
    confirmed: bool
    confirmation_reason: str


def _clip100(value: float) -> float:
    return max(0.0, min(100.0, value))


def _tier_for_score(score: float, cfg: HourReversalConfig) -> ReversalTier:
    if score >= cfg.strong_reversal_score:
        return ReversalTier.STRONG
    if score >= cfg.min_reversal_score:
        return ReversalTier.CANDIDATE
    if score >= cfg.watch_score:
        return ReversalTier.WATCH
    return ReversalTier.NONE


def _initial_direction(trend: TrendSnapshot, poll: PollSnapshot) -> Direction | None:
    if poll.dominant_side is not None and poll.dominant_poll is not None:
        if poll.dominant_poll >= 0.62:
            return Direction.UP if poll.dominant_side.value == "YES" else Direction.DOWN
    composite = trend.short_trend + 0.6 * trend.medium_trend
    if composite >= 0.0005:
        return Direction.UP
    if composite <= -0.0005:
        return Direction.DOWN
    return None


def assess_reversal(
    *,
    features: FeatureSnapshot,
    forecast: ProbabilityEstimate,
    trend: TrendSnapshot,
    vol: VolatilitySnapshot,
    poll: PollSnapshot,
    state: ContractReversalState,
    cfg: HourReversalConfig,
    supporting: SupportingAggregate | None = None,
) -> ReversalAssessment:
    initial = state.initial_direction
    if initial is None:
        initial = _initial_direction(trend, poll)

    reversal_direction = (
        Direction.DOWN if initial is Direction.UP else Direction.UP if initial is Direction.DOWN else None
    )
    favored_prob = (
        forecast.p_up if initial is Direction.UP else forecast.p_down if initial is Direction.DOWN else None
    )
    reversal_prob = (
        forecast.p_down if reversal_direction is Direction.DOWN else forecast.p_up if reversal_direction is Direction.UP else None
    )
    yes_poll = poll.yes_poll
    initial_poll = (
        yes_poll
        if initial is Direction.UP
        else (1.0 - yes_poll if yes_poll is not None else None)
        if initial is Direction.DOWN
        else None
    )
    probability_drop = (
        max(0.0, state.peak_model_prob - favored_prob)
        if state.established and favored_prob is not None
        else 0.0
    )
    kalshi_lag = None
    if initial_poll is not None and reversal_prob is not None:
        kalshi_lag = max(0.0, initial_poll - (1.0 - reversal_prob))

    momentum_exhaustion = 0.0
    if initial is Direction.UP:
        if trend.acceleration < 0 or trend.rate_of_change < 0:
            momentum_exhaustion += 35
        if features.trajectory in {
            TrajectoryState.DECELERATING_UP,
            TrajectoryState.REVERSING_DOWN,
        }:
            momentum_exhaustion += 35
        if features.late_momentum_fade > 0.35 or features.late_momentum_hammer > 0.35:
            momentum_exhaustion += 30
    elif initial is Direction.DOWN:
        if trend.acceleration > 0 or trend.rate_of_change > 0:
            momentum_exhaustion += 35
        if features.trajectory in {
            TrajectoryState.DECELERATING_DOWN,
            TrajectoryState.REVERSING_UP,
        }:
            momentum_exhaustion += 35
        if features.late_momentum_fade > 0.35 or features.late_momentum_hammer > 0.35:
            momentum_exhaustion += 30
    momentum_exhaustion = _clip100(momentum_exhaustion)

    structure_break = 0.0
    if initial is Direction.UP and trend.short_trend < trend.medium_trend:
        structure_break += 40
    if initial is Direction.DOWN and trend.short_trend > trend.medium_trend:
        structure_break += 40
    if features.trajectory in {TrajectoryState.REVERSING_UP, TrajectoryState.REVERSING_DOWN}:
        structure_break += 35
    if abs(features.z_distance_to_strike) < abs(state.initial_trend_strength * 1000):
        structure_break += 25
    structure_break = _clip100(structure_break)

    imbalance_shift = abs(features.orderbook_imbalance - state.last_orderbook_imbalance)
    volume_confirmation = _clip100(imbalance_shift * 120 + min(features.orderbook_imbalance, 1.0) * 20)

    order_flow_reversal = 0.0
    if initial is Direction.UP and features.orderbook_imbalance < -0.05:
        order_flow_reversal = _clip100(abs(features.orderbook_imbalance) * 100)
    elif initial is Direction.DOWN and features.orderbook_imbalance > 0.05:
        order_flow_reversal = _clip100(abs(features.orderbook_imbalance) * 100)

    cross_exchange_confirmation = 50.0
    if supporting is not None:
        cross_exchange_confirmation = _clip100(
            (1.0 - min(supporting.dispersion / 0.004, 1.0)) * 60
            + features.cross_venue_agreement * 40
        )
    elif features.cross_venue_agreement + 1e-12 >= 0.7:
        cross_exchange_confirmation = _clip100(features.cross_venue_agreement * 100)

    volatility_shift = _clip100(max(0.0, vol.vol_expansion) * 200 + vol.vol_contraction * -20 + 20)

    distance_from_strike = _clip100(
        100 - min(abs(features.z_distance_to_strike) * 35, 100)
    )

    minutes_left = features.seconds_remaining / 60.0
    if 5 <= minutes_left <= 25:
        time_remaining = 100.0
    elif 25 < minutes_left <= 40:
        time_remaining = 75.0
    elif minutes_left < 5:
        time_remaining = 40.0
    else:
        time_remaining = 50.0

    kalshi_repricing_lag = 0.0
    if kalshi_lag is not None:
        kalshi_repricing_lag = _clip100(kalshi_lag * 250)

    model_probability_change = _clip100(probability_drop * 250)

    components = ReversalScoreComponents(
        momentum_exhaustion=momentum_exhaustion,
        structure_break=structure_break,
        volume_confirmation=volume_confirmation,
        order_flow_reversal=order_flow_reversal,
        cross_exchange_confirmation=cross_exchange_confirmation,
        volatility_shift=volatility_shift,
        distance_from_strike=distance_from_strike,
        time_remaining=time_remaining,
        kalshi_repricing_lag=kalshi_repricing_lag,
        model_probability_change=model_probability_change,
    )
    score = sum(
        (
            components.momentum_exhaustion,
            components.structure_break,
            components.volume_confirmation,
            components.order_flow_reversal,
            components.cross_exchange_confirmation,
            components.volatility_shift,
            components.distance_from_strike,
            components.time_remaining,
            components.kalshi_repricing_lag,
            components.model_probability_change,
        )
    ) / 10.0
    tier = _tier_for_score(score, cfg)

    confirmed = (
        state.established
        and reversal_direction is not None
        and probability_drop + 1e-12 >= cfg.min_probability_flip
        and components.structure_break >= cfg.min_structure_break_score
        and components.model_probability_change >= cfg.min_model_change_score
        and components.kalshi_repricing_lag >= cfg.min_kalshi_lag_score
        and (kalshi_lag or 0.0) + 1e-12 >= cfg.min_kalshi_lag_cents
    )
    confirmation_reason = (
        "reversal confirmed"
        if confirmed
        else "awaiting exhaustion, structure break, model flip, or Kalshi lag"
    )
    summary = (
        f"{tier.value} {score:.0f}/100 · "
        f"{'↑' if initial is Direction.UP else '↓' if initial is Direction.DOWN else '?'}→"
        f"{'↓' if reversal_direction is Direction.DOWN else '↑' if reversal_direction is Direction.UP else '?'}"
        f" · drop {probability_drop:.0%}"
        + (f" · lag {kalshi_lag:.0%}" if kalshi_lag is not None else "")
    )
    return ReversalAssessment(
        score=round(score, 1),
        tier=tier,
        components=components,
        initial_direction=initial,
        reversal_direction=reversal_direction,
        reversal_side_probability=reversal_prob,
        kalshi_lag_cents=kalshi_lag,
        probability_drop=probability_drop if state.established else None,
        summary=summary,
        confirmed=confirmed,
        confirmation_reason=confirmation_reason,
    )
