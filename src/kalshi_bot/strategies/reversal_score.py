"""Reversal score (0–100) from momentum exhaustion, structure, flow, and Kalshi lag."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from kalshi_bot.domain import ContractSide, FeatureSnapshot, ProbabilityEstimate, Regime, TrajectoryState
from kalshi_bot.features.enriched import EnrichedFeatures
from kalshi_bot.market.orderbook import microprice


class ReversalTier(str, Enum):
    NONE = "no_reversal"  # 0–49
    WATCH = "watch"  # 50–69
    CANDIDATE = "reversal_candidate"  # 70–84
    STRONG = "strong_reversal_candidate"  # 85–100


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
class ReversalScoreAssessment:
    score: float
    tier: ReversalTier
    components: ReversalScoreComponents
    initial_direction: str  # UP or DOWN
    reversal_side: ContractSide
    kalshi_yes_poll: float | None
    model_p_up: float
    model_p_down: float
    probability_change: float
    kalshi_lag_on_reversal_side: float
    summary: str


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _initial_direction(features: FeatureSnapshot) -> str:
    z = features.z_distance_to_strike
    mom60 = features.changes.get(60, features.medium_trend)
    if z >= 0.25 or (features.short_trend > 0 and mom60 > 0):
        return "UP"
    if z <= -0.25 or (features.short_trend < 0 and mom60 < 0):
        return "DOWN"
    return "UP" if z >= 0 else "DOWN"


def _reversal_side(initial_direction: str) -> ContractSide:
    return ContractSide.NO if initial_direction == "UP" else ContractSide.YES


def _tier_for_score(score: float) -> ReversalTier:
    if score >= 85.0:
        return ReversalTier.STRONG
    if score >= 70.0:
        return ReversalTier.CANDIDATE
    if score >= 50.0:
        return ReversalTier.WATCH
    return ReversalTier.NONE


def compute_reversal_score(
    features: FeatureSnapshot,
    enriched: EnrichedFeatures,
    forecast: ProbabilityEstimate,
    *,
    market_yes_poll: float | None,
    regime: Regime,
    seconds_remaining: float,
    prior_p_up: float | None = None,
    min_initial_move_z: float = 0.50,
) -> ReversalScoreAssessment:
    """Build a 0–100 reversal score. Does not authorize trades by itself."""
    pa = enriched.price_action
    micro = enriched.microstructure
    initial = _initial_direction(features)
    reversal = _reversal_side(initial)

    # --- Momentum exhaustion (0–1) ---
    fade = features.late_momentum_fade
    hammer_drift = max(features.late_momentum_hammer, features.late_momentum_drift)
    had_strong_move = (
        abs(features.z_distance_to_strike) >= min_initial_move_z
        or abs(pa.momentum_60s) >= 0.0004
        or hammer_drift >= 0.35
    )
    momentum_declining = (
        abs(pa.momentum_15s) + 1e-12 < abs(pa.momentum_30s)
        and abs(pa.momentum_30s) + 1e-12 <= abs(pa.momentum_60s) + 0.0001
    )
    reversing_traj = features.trajectory in {
        TrajectoryState.REVERSING_UP,
        TrajectoryState.REVERSING_DOWN,
        TrajectoryState.DECELERATING_UP,
        TrajectoryState.DECELERATING_DOWN,
    }
    momentum_exhaustion = _clip(
        fade * 0.55
        + (0.25 if momentum_declining else 0.0)
        + (0.20 if reversing_traj else 0.0)
        + (0.15 if had_strong_move and fade > 0.15 else 0.0)
    )

    # --- Structure break (0–1) ---
    structure_break = 0.0
    if pa.fake_breakout:
        structure_break += 0.45
    if reversing_traj:
        structure_break += 0.25
    strike = (
        features.settlement_effective_strike
        if features.settlement_effective_strike is not None
        else features.strike
    )
    if initial == "UP" and features.current_price + 1e-9 < strike:
        structure_break += 0.35  # failed hold above strike
    if initial == "DOWN" and features.current_price > strike + 1e-9:
        structure_break += 0.35
    if pa.breakout_detected and pa.fake_breakout:
        structure_break += 0.15
    structure_break = _clip(structure_break)

    # --- Volume / activity (0–1) ---
    vol_velocity = _clip(micro.trade_velocity / 2.0)
    volume_deterioration = _clip(micro.cancellation_rate * 1.2)
    vol_price_weak = _clip(pa.volatility_expansion * 0.5) if momentum_declining else 0.0
    volume_confirmation = _clip(
        vol_velocity * 0.45 + volume_deterioration * 0.35 + vol_price_weak * 0.20
    )

    # --- Order-flow reversal (0–1) ---
    imbalance = micro.bid_ask_imbalance
    flow_opposes = (
        imbalance < -0.08 if initial == "UP" else imbalance > 0.08 if initial == "DOWN" else False
    )
    pressure_opposes = (
        micro.new_order_pressure < -0.05 if initial == "UP" else micro.new_order_pressure > 0.05
    )
    whale_opposes = micro.whale_detected and (
        (initial == "UP" and micro.whale_side and "NO" in micro.whale_side)
        or (initial == "DOWN" and micro.whale_side and "YES" in micro.whale_side)
    )
    order_flow_reversal = _clip(
        (0.40 if flow_opposes else 0.0)
        + (0.30 if pressure_opposes else 0.0)
        + (0.30 if whale_opposes else 0.0)
        + abs(imbalance) * 0.15
    )

    # --- Cross-exchange confirmation (0–1) ---
    agreement = features.cross_venue_agreement
    dispersion_penalty = _clip(features.cross_venue_dispersion / 0.003)
    feeds_confirm = agreement >= 0.55 and dispersion_penalty < 0.7
    cross_exchange_confirmation = _clip(agreement * 0.7 + (0.3 if feeds_confirm else 0.0))

    # --- Volatility shift (0–1) ---
    volatility_shift = _clip(
        pa.volatility_expansion / 2.0
        + min(features.realized_vol / 0.5, 1.0) * 0.25
        + (0.2 if regime in {Regime.HIGH_VOLATILITY, Regime.CHAOTIC_UNSTABLE} else 0.0)
    )

    # --- Distance from strike (0–1) ---
    distance_from_strike = _clip(abs(features.z_distance_to_strike) / 1.25)

    # --- Time remaining (0–1) — sweet spot ~3–10 minutes ---
    minutes = seconds_remaining / 60.0
    if 3.0 <= minutes <= 10.0:
        time_remaining = 1.0
    elif minutes < 3.0:
        time_remaining = _clip(minutes / 3.0) * 0.6
    else:
        time_remaining = _clip(1.0 - (minutes - 10.0) / 5.0, 0.3, 1.0)

    # --- Kalshi repricing lag (0–1) ---
    model_p_up = forecast.p_up
    model_p_down = forecast.p_down
    if market_yes_poll is None:
        kalshi_repricing_lag = 0.0
        lag_on_side = 0.0
    else:
        if reversal is ContractSide.NO:
            kalshi_no_poll = 1.0 - market_yes_poll
            lag_on_side = model_p_down - kalshi_no_poll
        else:
            lag_on_side = model_p_up - market_yes_poll
        kalshi_repricing_lag = _clip(abs(lag_on_side) / 0.20)

    # --- Model probability change (0–1) ---
    if prior_p_up is not None:
        if reversal is ContractSide.NO:
            probability_change = prior_p_up - model_p_up
        else:
            probability_change = model_p_up - prior_p_up
    else:
        # Infer shift vs crowd when no prior snapshot exists.
        if market_yes_poll is not None:
            probability_change = (
                (market_yes_poll - model_p_up) if initial == "UP" else (model_p_up - market_yes_poll)
            )
        else:
            probability_change = 0.0
    model_probability_change = _clip(abs(probability_change) / 0.25)

    weights = {
        "momentum_exhaustion": 18.0,
        "structure_break": 15.0,
        "volume_confirmation": 12.0,
        "order_flow_reversal": 12.0,
        "cross_exchange_confirmation": 10.0,
        "volatility_shift": 8.0,
        "distance_from_strike": 7.0,
        "time_remaining": 5.0,
        "kalshi_repricing_lag": 13.0,
        "model_probability_change": 11.0,
    }
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
    raw = (
        components.momentum_exhaustion * weights["momentum_exhaustion"]
        + components.structure_break * weights["structure_break"]
        + components.volume_confirmation * weights["volume_confirmation"]
        + components.order_flow_reversal * weights["order_flow_reversal"]
        + components.cross_exchange_confirmation * weights["cross_exchange_confirmation"]
        + components.volatility_shift * weights["volatility_shift"]
        + components.distance_from_strike * weights["distance_from_strike"]
        + components.time_remaining * weights["time_remaining"]
        + components.kalshi_repricing_lag * weights["kalshi_repricing_lag"]
        + components.model_probability_change * weights["model_probability_change"]
    )
    score = round(_clip(raw, 0.0, 100.0), 1)
    tier = _tier_for_score(score)
    summary = (
        f"REVERSAL {score:.0f}/100 · {tier.value} · "
        f"initial {initial} → {reversal.value} · "
        f"lag {lag_on_side:+.1%} · Δprob {probability_change:+.1%}"
    )
    return ReversalScoreAssessment(
        score=score,
        tier=tier,
        components=components,
        initial_direction=initial,
        reversal_side=reversal,
        kalshi_yes_poll=market_yes_poll,
        model_p_up=model_p_up,
        model_p_down=model_p_down,
        probability_change=probability_change,
        kalshi_lag_on_reversal_side=lag_on_side,
        summary=summary,
    )


def market_yes_poll_from_book(market_orderbook) -> float | None:
    return microprice(market_orderbook, ContractSide.YES)
