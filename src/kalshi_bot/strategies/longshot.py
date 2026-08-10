"""Longshot-only entry filters and cent-based exit rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from kalshi_bot.config import LongshotConfig
from kalshi_bot.domain import (
    ContractSide,
    ExecutionEstimate,
    GateFailure,
    MarketPosition,
    MarketSnapshot,
    ProbabilityEstimate,
    utc_datetime,
)
from kalshi_bot.execution.stop_loss import executable_exit_price


@dataclass(frozen=True)
class LongshotExitConfig:
    take_profit_cents: float = 0.06
    take_profit_pct: float = 0.10
    take_profit_price: float = 0.55
    stop_loss_cents: float = 0.07
    stop_loss_pct: float = 0.10
    time_stop_seconds: float = 900.0
    reversal_cents: float = 0.05
    reversal_window_seconds: float = 120.0


@dataclass(frozen=True)
class LongshotExitSignal:
    should_exit: bool
    reason: str
    trigger: str
    exit_bid: float | None = None


def _failure(gate: str, reason: str, observed: object, required: object) -> GateFailure:
    return GateFailure(gate=gate, reason=reason, observed=observed, required=required)


def filter_longshot_executions(
    executions: dict[ContractSide, ExecutionEstimate],
    *,
    max_entry_price: float,
) -> dict[ContractSide, ExecutionEstimate]:
    return {
        side: execution
        for side, execution in executions.items()
        if execution.executable_cost + 1e-12 < max_entry_price
    }


def longshot_price_gate(
    executions: dict[ContractSide, ExecutionEstimate],
    *,
    cfg: LongshotConfig,
) -> GateFailure | None:
    if not executions:
        return _failure(
            "longshot_price",
            "no executable longshot quote below maximum entry price",
            None,
            cfg.max_entry_price,
        )
    return None


def evaluate_longshot_exit(
    *,
    market: MarketSnapshot,
    position: MarketPosition,
    quantity: float,
    cfg: LongshotExitConfig,
    now: datetime | None = None,
) -> LongshotExitSignal | None:
    """Cent-based take-profit, stop-loss, reversal, and time-stop exits."""
    if position.quantity <= 0:
        return None

    observed_now = utc_datetime(now or datetime.now(timezone.utc))
    exit_qty = min(position.quantity, quantity)
    exit_bid = executable_exit_price(market.orderbook, position.side, exit_qty)
    if exit_bid is None:
        return None

    entry = position.average_price
    gain = exit_bid - entry
    held_seconds = 0.0
    if position.opened_at is not None:
        held_seconds = (observed_now - utc_datetime(position.opened_at)).total_seconds()

    if gain + 1e-12 >= cfg.take_profit_cents:
        return LongshotExitSignal(
            should_exit=True,
            reason=(
                f"longshot take profit: +{gain * 100:.0f}¢ "
                f"(target +{cfg.take_profit_cents * 100:.0f}¢)"
            ),
            trigger="take_profit_cents",
            exit_bid=exit_bid,
        )

    if entry > 0 and gain / entry + 1e-12 >= cfg.take_profit_pct:
        return LongshotExitSignal(
            should_exit=True,
            reason=(
                f"longshot take profit: +{gain / entry:.0%} "
                f"(target +{cfg.take_profit_pct:.0%})"
            ),
            trigger="take_profit_pct",
            exit_bid=exit_bid,
        )

    if exit_bid + 1e-12 >= cfg.take_profit_price:
        return LongshotExitSignal(
            should_exit=True,
            reason=(
                f"longshot take profit: bid {exit_bid:.2f} "
                f"reached target {cfg.take_profit_price:.2f}"
            ),
            trigger="take_profit_price",
            exit_bid=exit_bid,
        )

    if gain <= -cfg.stop_loss_cents - 1e-12:
        return LongshotExitSignal(
            should_exit=True,
            reason=(
                f"longshot stop loss: {gain * 100:.0f}¢ "
                f"(limit -{cfg.stop_loss_cents * 100:.0f}¢)"
            ),
            trigger="stop_loss_cents",
            exit_bid=exit_bid,
        )

    if entry > 0 and (-gain) / entry + 1e-12 >= cfg.stop_loss_pct:
        return LongshotExitSignal(
            should_exit=True,
            reason=(
                f"longshot stop loss: {-gain / entry:.0%} "
                f"(limit -{cfg.stop_loss_pct:.0%})"
            ),
            trigger="stop_loss_pct",
            exit_bid=exit_bid,
        )

    if (
        held_seconds + 1e-9 <= cfg.reversal_window_seconds
        and gain <= -cfg.reversal_cents - 1e-12
    ):
        return LongshotExitSignal(
            should_exit=True,
            reason=(
                f"longshot reversal exit: {gain * 100:.0f}¢ within "
                f"{cfg.reversal_window_seconds:.0f}s of entry"
            ),
            trigger="reversal_check",
            exit_bid=exit_bid,
        )

    if (
        cfg.time_stop_seconds > 0
        and held_seconds + 1e-9 >= cfg.time_stop_seconds
        and gain <= 1e-12
    ):
        return LongshotExitSignal(
            should_exit=True,
            reason=(
                f"longshot time stop: held {held_seconds:.0f}s without profit "
                f"(limit {cfg.time_stop_seconds:.0f}s)"
            ),
            trigger="time_stop",
            exit_bid=exit_bid,
        )

    return None


def longshot_exit_config(cfg: LongshotConfig) -> LongshotExitConfig:
    return LongshotExitConfig(
        take_profit_cents=cfg.take_profit_cents,
        take_profit_pct=cfg.take_profit_pct,
        take_profit_price=cfg.take_profit_price,
        stop_loss_cents=cfg.stop_loss_cents,
        stop_loss_pct=cfg.stop_loss_pct,
        time_stop_seconds=cfg.time_stop_seconds,
        reversal_cents=cfg.reversal_cents,
        reversal_window_seconds=cfg.reversal_window_seconds,
    )
