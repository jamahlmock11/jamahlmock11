"""Entry anti-fakeout filters: persistence, chop zone, and window regime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from kalshi_bot.domain import (
    ContractSide,
    DecisionAction,
    DecisionResult,
    FeatureSnapshot,
    GateFailure,
)


class WindowRegimeKind(str, Enum):
    TRENDING = "trending"
    CHOPPY = "choppy"
    MEAN_REVERTING = "mean_reverting"


def classify_window_regime(features: FeatureSnapshot) -> WindowRegimeKind:
    """
    Classify the recent 5–10 minute window as trending vs choppy.

    Uses realized volatility, short/medium trend alignment, and longer-horizon
    moves so momentum-heavy signals are down-weighted in chop.
    """
    trend_return = 0.0004
    short = features.short_trend
    medium = features.medium_trend
    move_5m = features.changes.get(300, features.changes.get(180, medium))
    move_10m = features.changes.get(600, move_5m)

    def direction(value: float) -> int:
        if value > trend_return:
            return 1
        if value < -trend_return:
            return -1
        return 0

    short_dir = direction(short)
    medium_dir = direction(medium)
    long_dir = direction(move_5m)
    directions = [value for value in (short_dir, medium_dir, long_dir) if value != 0]
    aligned = len(directions) >= 2 and len(set(directions)) == 1
    conflicting = (
        short_dir != 0
        and medium_dir != 0
        and short_dir != medium_dir
    )
    low_conviction = abs(short) < trend_return and abs(medium) < trend_return
    elevated_vol = features.realized_vol >= 0.50
    long_flat = abs(move_5m) < trend_return and abs(move_10m) < trend_return

    if conflicting or (low_conviction and elevated_vol):
        return WindowRegimeKind.CHOPPY
    if elevated_vol and long_flat:
        return WindowRegimeKind.CHOPPY
    if aligned and abs(short) >= trend_return:
        return WindowRegimeKind.TRENDING
    if abs(features.mean_reversion_score) > 1.0 and low_conviction:
        return WindowRegimeKind.MEAN_REVERTING
    return WindowRegimeKind.CHOPPY if low_conviction else WindowRegimeKind.TRENDING


def is_in_chop_zone(features: FeatureSnapshot, min_sigma: float) -> bool:
    """Return True when spot is too close to strike for reliable directional edge."""
    if min_sigma <= 0:
        return False
    return abs(features.z_distance_to_strike) + 1e-12 < min_sigma


@dataclass
class EntrySignalTracker:
    """Require directional edge to persist across consecutive polls before entry."""

    required_polls: int = 3
    _ticker: str | None = field(default=None, repr=False)
    _consecutive: int = field(default=0, repr=False)
    _side: ContractSide | None = field(default=None, repr=False)

    def reset(self) -> None:
        self._ticker = None
        self._consecutive = 0
        self._side = None

    def observe_signal(
        self,
        *,
        ticker: str,
        side: ContractSide | None,
        edge: float | None,
        required_edge: float | None,
    ) -> int:
        """Track consecutive polls where side + edge clear the minimum."""
        if ticker != self._ticker:
            self._ticker = ticker
            self._consecutive = 0
            self._side = None

        qualifies = (
            side is not None
            and edge is not None
            and required_edge is not None
            and edge + 1e-12 >= required_edge
        )
        if not qualifies:
            self._consecutive = 0
            self._side = None
            return 0

        if self._side is not None and side != self._side:
            self._consecutive = 1
            self._side = side
            return self._consecutive

        self._side = side
        self._consecutive += 1
        return self._consecutive

    def ready(self, *, ticker: str) -> bool:
        if self.required_polls <= 1:
            return True
        return self._ticker == ticker and self._consecutive >= self.required_polls


def apply_signal_persistence_gate(
    decision: DecisionResult,
    *,
    ticker: str,
    tracker: EntrySignalTracker,
) -> DecisionResult:
    """Block entries until the directional edge has persisted long enough."""
    tracker.observe_signal(
        ticker=ticker,
        side=decision.selected_side,
        edge=decision.edge,
        required_edge=decision.required_edge,
    )
    if decision.action not in {DecisionAction.BUY_UP, DecisionAction.BUY_DOWN}:
        return decision
    if tracker.ready(ticker=ticker):
        return decision

    streak = tracker._consecutive
    reason = (
        f"signal persistence: {streak}/{tracker.required_polls} consecutive polls "
        f"with edge on {decision.selected_side.value if decision.selected_side else 'n/a'}"
    )
    return DecisionResult(
        action=DecisionAction.NO_TRADE,
        reason=reason,
        gate_failures=decision.gate_failures
        + (
            GateFailure(
                gate="signal_persistence",
                reason="directional edge must persist across consecutive polls",
                observed=streak,
                required=tracker.required_polls,
            ),
        ),
        current_direction=decision.current_direction,
        predicted_direction=decision.predicted_direction,
        trade_direction=decision.trade_direction,
        selected_side=decision.selected_side,
        predicted_probability=decision.predicted_probability,
        executable_cost=decision.executable_cost,
        edge=decision.edge,
        target_edge=decision.target_edge,
        required_edge=decision.required_edge,
        trade_tier=decision.trade_tier,
        entry_timing=decision.entry_timing,
        size_multiplier=decision.size_multiplier,
        quantity=decision.quantity,
        execution=decision.execution,
        forecast_alignment=decision.forecast_alignment,
    )
