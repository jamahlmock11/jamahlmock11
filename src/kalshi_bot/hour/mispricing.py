"""Compare terminal expiration probabilities to executable Kalshi prices."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.config import TerminalProbabilityConfig
from kalshi_bot.domain import ContractSide, OrderBookSnapshot
from kalshi_bot.hour.terminal_probability import TerminalForecast
from kalshi_bot.market.orderbook import (
    InsufficientDepthError,
    depth,
    estimate_buy_execution,
    spread,
)


@dataclass(frozen=True)
class SideMispricing:
    side: ContractSide
    model_probability: float
    ask_price: float | None
    executable_cost: float
    raw_edge: float
    estimated_costs: float
    net_edge: float


@dataclass(frozen=True)
class MispricingAssessment:
    yes: SideMispricing | None
    no: SideMispricing | None
    best_side: ContractSide | None
    best_net_edge: float
    best_raw_edge: float
    required_edge: float
    yes_net_edge: float | None
    no_net_edge: float | None


def required_edge_for_minutes(
    minutes_remaining: float,
    cfg: TerminalProbabilityConfig,
) -> float:
    minutes = max(minutes_remaining, 0.0)
    if not cfg.dynamic_edge_enabled:
        return cfg.fallback_min_edge
    for band in cfg.dynamic_edge_bands:
        if band.min_minutes <= minutes < band.max_minutes:
            return band.min_edge
    if cfg.dynamic_edge_bands:
        return cfg.dynamic_edge_bands[-1].min_edge
    return cfg.fallback_min_edge


def terminal_hard_min_edge(cfg: TerminalProbabilityConfig) -> float:
    """Lowest tier floor for risk execution when dynamic bands are enabled."""
    if cfg.dynamic_edge_enabled and cfg.dynamic_edge_bands:
        return min(band.min_edge for band in cfg.dynamic_edge_bands)
    return cfg.fallback_min_edge


def _execution_costs(
    executable_cost: float,
    *,
    fee_rate: float,
    fee_per_contract: float,
    slippage_bps: float,
    slippage_per_contract: float,
) -> float:
    slippage = executable_cost * slippage_bps / 10_000.0 + slippage_per_contract
    fees = executable_cost * fee_rate + fee_per_contract
    return slippage + fees


def assess_mispricing(
    terminal: TerminalForecast,
    orderbook: OrderBookSnapshot,
    *,
    quantity: float,
    cfg: TerminalProbabilityConfig,
    fee_rate: float = 0.0,
    fee_per_contract: float = 0.0,
    slippage_bps: float = 5.0,
    slippage_per_contract: float = 0.0,
) -> MispricingAssessment:
    """Evaluate net mispricing on BOTH YES and NO using calibrated terminal probabilities."""
    minutes_remaining = terminal.seconds_remaining / 60.0
    required = required_edge_for_minutes(minutes_remaining, cfg)

    sides: dict[ContractSide, SideMispricing | None] = {}
    for side, model_prob in (
        (ContractSide.YES, terminal.calibrated_p_yes),
        (ContractSide.NO, terminal.calibrated_p_no),
    ):
        ask = orderbook.yes_ask if side is ContractSide.YES else orderbook.no_ask
        try:
            execution = estimate_buy_execution(
                orderbook,
                side,
                quantity,
                fee_rate=fee_rate,
                fee_per_contract=fee_per_contract,
                slippage_bps=slippage_bps,
                slippage_per_contract=slippage_per_contract,
            )
            costs = _execution_costs(
                execution.executable_cost,
                fee_rate=fee_rate,
                fee_per_contract=fee_per_contract,
                slippage_bps=slippage_bps,
                slippage_per_contract=slippage_per_contract,
            )
            entry_price = ask if ask is not None else execution.executable_cost
            raw_edge = model_prob - entry_price
            net_edge = model_prob - execution.executable_cost - costs
            sides[side] = SideMispricing(
                side=side,
                model_probability=model_prob,
                ask_price=ask,
                executable_cost=execution.executable_cost,
                raw_edge=raw_edge,
                estimated_costs=costs,
                net_edge=net_edge,
            )
        except InsufficientDepthError:
            sides[side] = None

    yes_side = sides.get(ContractSide.YES)
    no_side = sides.get(ContractSide.NO)
    candidates = [item for item in (yes_side, no_side) if item is not None]
    best = max(candidates, key=lambda item: item.raw_edge) if candidates else None

    return MispricingAssessment(
        yes=yes_side,
        no=no_side,
        best_side=best.side if best is not None else None,
        best_net_edge=best.net_edge if best is not None else -1.0,
        best_raw_edge=best.raw_edge if best is not None else -1.0,
        required_edge=required,
        yes_net_edge=yes_side.net_edge if yes_side is not None else None,
        no_net_edge=no_side.net_edge if no_side is not None else None,
    )


def liquidity_ok(
    orderbook: OrderBookSnapshot,
    *,
    max_spread: float,
    quantity: float,
    require_depth: bool,
) -> tuple[bool, str]:
    for side in (ContractSide.YES, ContractSide.NO):
        side_spread = spread(orderbook, side)
        if side_spread is None or side_spread > max_spread:
            return False, f"{side.value} spread too wide"
        if require_depth and depth(orderbook, side, asks=True) < quantity:
            return False, f"{side.value} ask depth insufficient"
    return True, "liquidity ok"
