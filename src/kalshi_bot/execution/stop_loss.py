"""Stop-loss and thesis-based exit helpers for open binary positions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from kalshi_bot.domain import (
    ContractSide,
    FeatureSnapshot,
    GateFailure,
    MarketPosition,
    MarketSnapshot,
    OrderBookSnapshot,
    ProbabilityEstimate,
    utc_datetime,
)
from kalshi_bot.market.orderbook import depth


@dataclass(frozen=True)
class PositionExitSignal:
    should_exit: bool
    reason: str
    trigger: str
    premium_loss_fraction: float | None = None
    exit_bid: float | None = None


def premium_loss_fraction(entry_price: float, exit_bid: float) -> float:
    """Loss as a fraction of entry premium (0.45 = 45% of premium lost)."""
    if entry_price <= 0:
        return 0.0
    return max(0.0, (entry_price - exit_bid) / entry_price)


def executable_exit_price(
    book: OrderBookSnapshot,
    side: ContractSide,
    quantity: float,
) -> float | None:
    """Volume-weighted bid price available to sell ``quantity`` contracts."""
    if quantity <= 0 or depth(book, side, asks=False) + 1e-12 < quantity:
        return None
    bids = book.levels(side, asks=False)
    if not bids:
        return None
    remaining = quantity
    proceeds = 0.0
    filled = 0.0
    for level in bids:
        take = min(remaining, level.size)
        if take <= 0:
            continue
        proceeds += take * level.price
        filled += take
        remaining -= take
        if remaining <= 1e-12:
            break
    if filled <= 0:
        return None
    return proceeds / filled


def thesis_reversal_triggered(
    position: MarketPosition,
    forecast: ProbabilityEstimate,
    *,
    margin: float,
) -> bool:
    """True only when the opposite side leads by at least ``margin`` probability points."""
    if margin <= 0:
        return position.side is not (
            ContractSide.YES if forecast.p_up >= forecast.p_down else ContractSide.NO
        )
    if position.side is ContractSide.YES:
        return forecast.p_down + 1e-12 >= forecast.p_up + margin
    if position.side is ContractSide.NO:
        return forecast.p_up + 1e-12 >= forecast.p_down + margin
    return False


def evaluate_position_exit(
    *,
    market: MarketSnapshot,
    position: MarketPosition,
    forecast: ProbabilityEstimate,
    failures: tuple[GateFailure, ...] | list[GateFailure],
    predicted_side: ContractSide,
    quantity: float,
    stop_loss_fraction: float,
    opposite_edge_shift: float = 0.15,
    thesis_reversal_margin: float = 0.10,
    min_hold_seconds: float = 0.0,
    now: datetime | None = None,
    reliability_gates: set[str] | frozenset[str] | None = None,
) -> PositionExitSignal | None:
    """Return an exit signal when stop-loss or thesis protections trigger."""
    if position.quantity <= 0:
        return None

    observed_now = utc_datetime(now or datetime.now(timezone.utc))
    within_min_hold = False
    if min_hold_seconds > 0 and position.opened_at is not None:
        held_seconds = (observed_now - utc_datetime(position.opened_at)).total_seconds()
        within_min_hold = held_seconds + 1e-9 < min_hold_seconds

    gates = reliability_gates or frozenset(
        {
            "market_validity",
            "market_status",
            "time_window",
            "primary_brti",
            "live_data",
            "proxy_constituents",
            "proxy_dispersion",
            "proxy_late_contract",
            "benchmark_freshness",
            "feature_freshness",
            "data_completeness",
            "confidence",
            "agreement",
            "risk_lock",
        }
    )
    exit_qty = min(position.quantity, quantity)
    exit_bid = executable_exit_price(market.orderbook, position.side, exit_qty)
    entry = position.average_price

    if stop_loss_fraction > 0 and exit_bid is not None:
        loss = premium_loss_fraction(entry, exit_bid)
        if loss + 1e-12 >= stop_loss_fraction:
            return PositionExitSignal(
                should_exit=True,
                reason=(
                    f"stop loss: {loss:.0%} premium loss "
                    f"(limit {stop_loss_fraction:.0%}; entry {entry:.2f} bid {exit_bid:.2f})"
                ),
                trigger="stop_loss",
                premium_loss_fraction=loss,
                exit_bid=exit_bid,
            )

    held_prob = forecast.p_up if position.side is ContractSide.YES else forecast.p_down
    thesis_reversed = thesis_reversal_triggered(
        position,
        forecast,
        margin=thesis_reversal_margin,
    )
    opposite_edge_better = False
    if position.side is ContractSide.YES and forecast.p_down > held_prob + opposite_edge_shift:
        opposite_edge_better = True
    if position.side is ContractSide.NO and forecast.p_up > held_prob + opposite_edge_shift:
        opposite_edge_better = True
    unreliable = any(failure.gate in gates for failure in failures)

    if thesis_reversed or opposite_edge_better or unreliable:
        if within_min_hold:
            return None
        if thesis_reversed:
            reason = (
                f"forecast reversed the held thesis "
                f"(opposite lead ≥{thesis_reversal_margin:.0%})"
            )
            trigger = "thesis_reversal"
        elif opposite_edge_better:
            reason = "opposite side developed stronger edge"
            trigger = "opposite_edge"
        else:
            reason = "held thesis lost reliable data"
            trigger = "unreliable_data"
        return PositionExitSignal(
            should_exit=True,
            reason=f"{reason}; exit before any opposite entry",
            trigger=trigger,
            premium_loss_fraction=(
                premium_loss_fraction(entry, exit_bid) if exit_bid is not None else None
            ),
            exit_bid=exit_bid,
        )

    return None
