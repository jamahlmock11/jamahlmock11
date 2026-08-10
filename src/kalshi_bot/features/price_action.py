"""Price-action features: VWAP, momentum, volatility, support/resistance, breakouts."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from kalshi_bot.domain import FeatureSnapshot, Regime, TrajectoryState


@dataclass(frozen=True)
class PriceActionSnapshot:
    """Short-horizon price action metrics."""

    vwap_distance_pct: float
    momentum_15s: float
    momentum_30s: float
    momentum_60s: float
    volatility_expansion: float  # >1 = expanding, <1 = contracting
    recent_high: float
    recent_low: float
    support_distance_pct: float
    resistance_distance_pct: float
    breakout_detected: bool
    fake_breakout: bool
    breakout_direction: str | None


def _return_at(changes: dict[int, float], horizon: int) -> float:
    return changes.get(horizon, 0.0)


def compute_price_action(
    features: FeatureSnapshot,
    regime: Regime,
) -> PriceActionSnapshot:
    changes = dict(features.changes)
    momentum_15s = _return_at(changes, 15)
    momentum_30s = _return_at(changes, 30)
    momentum_60s = _return_at(changes, 60)

    # VWAP distance: price vs rolling mean implied by mean-reversion score
    vwap_distance_pct = -features.mean_reversion_score * features.current_price * 0.01
    if features.current_price > 0:
        vwap_distance_pct = (
            features.mean_reversion_score * features.current_price / features.current_price
        ) * 100.0
    vwap_distance_pct = -features.mean_reversion_score

    # Volatility expansion: short vs medium realized move
    short_vol = abs(momentum_15s) + abs(momentum_30s)
    medium_vol = abs(momentum_60s) + abs(features.medium_trend)
    volatility_expansion = short_vol / max(medium_vol, 1e-8)

    # Support/resistance from recent price range (proxy via strike distance and trends)
    move_band = max(features.expected_remaining_move, features.current_price * 0.0005)
    recent_high = features.current_price + move_band * max(0.0, features.z_distance_to_strike)
    recent_low = features.current_price - move_band * max(0.0, -features.z_distance_to_strike)
    # Use horizon returns to refine range
    all_returns = list(changes.values())
    if all_returns:
        cumulative = features.current_price
        prices = [cumulative]
        for r in sorted(changes):
            cumulative *= 1.0 + changes[r]
            prices.append(cumulative)
        recent_high = max(prices + [recent_high])
        recent_low = min(prices + [recent_low])

    support_distance_pct = (
        (features.current_price - recent_low) / features.current_price * 100.0
        if features.current_price > 0
        else 0.0
    )
    resistance_distance_pct = (
        (recent_high - features.current_price) / features.current_price * 100.0
        if features.current_price > 0
        else 0.0
    )

    breakout_detected = regime in {Regime.BREAKOUT, Regime.BREAKDOWN}
    breakout_direction = None
    if breakout_detected:
        breakout_direction = "UP" if regime is Regime.BREAKOUT else "DOWN"

    # Fake breakout: breakout regime but momentum fading (deceleration)
    fake_breakout = False
    if breakout_detected:
        fading = features.trajectory in {
            TrajectoryState.DECELERATING_UP,
            TrajectoryState.DECELERATING_DOWN,
        }
        weak_momentum = abs(momentum_15s) < abs(momentum_60s) * 0.5
        fake_breakout = fading or weak_momentum

    return PriceActionSnapshot(
        vwap_distance_pct=vwap_distance_pct,
        momentum_15s=momentum_15s,
        momentum_30s=momentum_30s,
        momentum_60s=momentum_60s,
        volatility_expansion=volatility_expansion,
        recent_high=recent_high,
        recent_low=recent_low,
        support_distance_pct=support_distance_pct,
        resistance_distance_pct=resistance_distance_pct,
        breakout_detected=breakout_detected,
        fake_breakout=fake_breakout,
        breakout_direction=breakout_direction,
    )
