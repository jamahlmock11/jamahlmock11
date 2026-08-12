"""Longshot-only entry filters and cent-based exit rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from kalshi_bot.config import LongshotConfig
from kalshi_bot.domain import (
    ContractSide,
    ExecutionEstimate,
    FeatureSnapshot,
    GateFailure,
    MarketPosition,
    MarketSnapshot,
    ProbabilityEstimate,
    utc_datetime,
)
from kalshi_bot.execution.stop_loss import executable_exit_price
from kalshi_bot.market.poll_alignment import PollSnapshot
from kalshi_bot.strategies.crowd_strike_hold import (
    CrowdStrikeHoldAssessment,
    crowd_strike_hold_gate,
    evaluate_crowd_strike_hold,
)


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
    inclusive_max: bool = False,
) -> dict[ContractSide, ExecutionEstimate]:
    if inclusive_max:
        return {
            side: execution
            for side, execution in executions.items()
            if execution.executable_cost <= max_entry_price + 1e-12
        }
    return {
        side: execution
        for side, execution in executions.items()
        if execution.executable_cost + 1e-12 < max_entry_price
    }


def filter_crowd_follow_executions(
    executions: dict[ContractSide, ExecutionEstimate],
    *,
    min_entry_price: float,
    max_entry_price: float,
) -> dict[ContractSide, ExecutionEstimate]:
    return {
        side: execution
        for side, execution in executions.items()
        if execution.executable_cost + 1e-12 >= min_entry_price
        and execution.executable_cost <= max_entry_price + 1e-12
    }


@dataclass(frozen=True)
class CrowdFollowMode:
    active: bool = False
    late_relaxed: bool = False
    favorite_max_price: float | None = None


@dataclass(frozen=True)
class LongshotEntryContext:
    executions: dict[ContractSide, ExecutionEstimate]
    max_entry_price: float
    min_entry_price: float | None
    min_edge_override: float | None
    forced_side: ContractSide | None
    extreme_poll_active: bool
    strike_hold: CrowdStrikeHoldAssessment | None
    failures: tuple[GateFailure, ...]


def resolve_crowd_follow_mode(
    *,
    poll: PollSnapshot,
    seconds_remaining: float,
    cfg: LongshotConfig,
) -> CrowdFollowMode:
    """Resolve whether crowd-follow is active (poll + time only, no model gates)."""
    inactive = CrowdFollowMode()
    if not cfg.follow_extreme_poll:
        return inactive
    if poll.dominant_poll is None or poll.dominant_side is None:
        return inactive

    in_late = (
        cfg.late_crowd_follow_seconds > 0
        and seconds_remaining + 1e-9 <= cfg.late_crowd_follow_seconds
    )
    if in_late and poll.dominant_poll + 1e-12 >= cfg.late_crowd_poll_threshold:
        return CrowdFollowMode(
            active=True,
            late_relaxed=True,
            favorite_max_price=cfg.late_crowd_favorite_max_price,
        )

    if poll.dominant_poll + 1e-12 >= cfg.extreme_poll_threshold:
        if (
            cfg.extreme_poll_late_seconds <= 0
            or seconds_remaining <= cfg.extreme_poll_late_seconds + 1e-6
        ):
            return CrowdFollowMode(
                active=True,
                late_relaxed=False,
                favorite_max_price=cfg.extreme_favorite_max_price,
            )
    return inactive


def extreme_poll_active(
    *,
    poll: PollSnapshot,
    seconds_remaining: float,
    cfg: LongshotConfig,
) -> bool:
    return resolve_crowd_follow_mode(
        poll=poll,
        seconds_remaining=seconds_remaining,
        cfg=cfg,
    ).active


def resolve_longshot_entries(
    executions: dict[ContractSide, ExecutionEstimate],
    *,
    poll: PollSnapshot,
    forecast: ProbabilityEstimate,
    seconds_remaining: float,
    cfg: LongshotConfig,
    poll_cfg: object | None = None,
    features: FeatureSnapshot | None = None,
) -> LongshotEntryContext:
    """Apply crowd-follow filters from market poll, time, price, and strike path."""
    del forecast, poll_cfg
    failures: list[GateFailure] = []
    max_price = cfg.max_entry_price
    min_price: float | None = None
    min_edge_override: float | None = None
    forced_side: ContractSide | None = None
    use_crowd_price_band = False

    crowd_mode = resolve_crowd_follow_mode(
        poll=poll,
        seconds_remaining=seconds_remaining,
        cfg=cfg,
    )
    follow_favorite = crowd_mode.active
    strike_hold: CrowdStrikeHoldAssessment | None = None

    if follow_favorite:
        dominant = poll.dominant_side
        assert dominant is not None
        executions = {
            side: execution
            for side, execution in executions.items()
            if side is dominant
        }
        favorite_poll = poll.dominant_poll
        if cfg.crowd_follow_price_band_cents > 0 and favorite_poll is not None:
            use_crowd_price_band = True
            band = cfg.crowd_follow_price_band_cents
            min_price = max(0.0, favorite_poll - band)
            max_price = min(1.0, favorite_poll + band)
        else:
            max_price = (
                crowd_mode.favorite_max_price
                if crowd_mode.favorite_max_price is not None
                else cfg.extreme_favorite_max_price
            )
        forced_side = dominant
        min_edge_override = -1.0
        if crowd_mode.late_relaxed and features is not None:
            strike_hold = evaluate_crowd_strike_hold(
                features,
                crowd_side=dominant,
                cfg=cfg,
            )
            hold_failure = crowd_strike_hold_gate(strike_hold, cfg=cfg)
            if hold_failure is not None:
                failures.append(hold_failure)
    elif cfg.favorite_only:
        failures.append(
            _failure(
                "favorite_only",
                "plan B only: entries require market favorite at or above poll threshold",
                poll.dominant_poll,
                cfg.extreme_poll_threshold,
            )
        )
        executions = {}
    else:
        price_failure = longshot_price_gate(executions, cfg=cfg)
        if price_failure is not None:
            failures.append(price_failure)

    if use_crowd_price_band and min_price is not None:
        filtered = filter_crowd_follow_executions(
            executions,
            min_entry_price=min_price,
            max_entry_price=max_price,
        )
        price_failure_reason = (
            "no executable quote within crowd-follow price band around favorite"
        )
        price_failure_required = f"{min_price:.2f}-{max_price:.2f}"
    else:
        filtered = filter_longshot_executions(
            executions,
            max_entry_price=max_price,
            inclusive_max=forced_side is not None,
        )
        price_failure_reason = "no executable quote below maximum entry price"
        price_failure_required = max_price
    if not filtered and executions:
        failures.append(
            _failure(
                "longshot_price",
                price_failure_reason,
                {side: execution.executable_cost for side, execution in executions.items()},
                price_failure_required,
            )
        )

    return LongshotEntryContext(
        executions=filtered,
        max_entry_price=max_price,
        min_entry_price=min_price,
        min_edge_override=min_edge_override,
        forced_side=forced_side,
        extreme_poll_active=follow_favorite,
        strike_hold=strike_hold,
        failures=tuple(failures),
    )


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
