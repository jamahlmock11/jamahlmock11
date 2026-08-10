"""ProbabilityModelAgent — compares model vs market and applies edge gate."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.domain import ContractSide, DecisionAction, DecisionResult, ProbabilityEstimate
from kalshi_bot.market.orderbook import microprice


@dataclass(frozen=True)
class ProbabilityVerdict:
    model_probability: float
    market_probability: float
    edge: float
    selected_side: ContractSide
    action: str
    reason: str


def evaluate_probability_model(
    forecast: ProbabilityEstimate,
    *,
    yes_mid: float,
    decision: DecisionResult | None,
    min_edge: float,
) -> ProbabilityVerdict:
    side = ContractSide.YES if forecast.p_up >= forecast.p_down else ContractSide.NO
    model_prob = forecast.p_up if side is ContractSide.YES else forecast.p_down
    market_prob = yes_mid if side is ContractSide.YES else 1.0 - yes_mid
    edge = model_prob - market_prob

    if decision is not None and decision.edge is not None:
        edge = decision.edge
        if decision.selected_side is not None:
            side = decision.selected_side
            model_prob = forecast.p_up if side is ContractSide.YES else forecast.p_down
            market_prob = yes_mid if side is ContractSide.YES else 1.0 - yes_mid

    if edge + 1e-12 < min_edge:
        action = DecisionAction.NO_TRADE.value
        reason = f"edge below {min_edge:.0%} threshold"
    elif decision is not None and decision.action in {DecisionAction.BUY_UP, DecisionAction.BUY_DOWN}:
        action = decision.action.value
        reason = decision.reason
    else:
        action = DecisionAction.NO_TRADE.value
        reason = f"edge below {min_edge:.0%} threshold"

    return ProbabilityVerdict(
        model_probability=model_prob,
        market_probability=market_prob,
        edge=edge,
        selected_side=side,
        action=action,
        reason=reason,
    )


def market_yes_mid(market) -> float:
    mid = microprice(market.orderbook, ContractSide.YES)
    if mid is not None:
        return mid
    if market.yes_bid is not None and market.yes_ask is not None:
        return (market.yes_bid + market.yes_ask) / 2.0
    return market.yes_ask or market.yes_bid or 0.5
