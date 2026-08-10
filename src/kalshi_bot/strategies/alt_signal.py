"""Shared types for alternative execution strategies."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.domain import ContractSide


@dataclass(frozen=True)
class AltTradeSignal:
    strategy: str
    ticker: str
    side: ContractSide
    action: str
    quantity: float
    limit_price: float
    edge: float
    time_in_force: str
    reason: str
    intent_id: str
    rationale: str = ""
