"""Position sizing and risk limits."""

from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_bot.config import AppConfig
from kalshi_bot.models.probability import Confidence, EdgeSignal


@dataclass
class RiskState:
    realized_pnl: float = 0.0
    open_exposure_usd: float = 0.0
    trades_today: int = 0
    halted: bool = False
    halt_reason: str = ""
    positions: dict[str, float] = field(default_factory=dict)


class RiskManager:
    def __init__(self, config: AppConfig, max_daily_loss: float = 100.0):
        self.config = config
        self.max_daily_loss = max_daily_loss
        self.state = RiskState()

    def size_mispricing(self, signal: EdgeSignal) -> int:
        if self.state.halted:
            return 0
        if signal.confidence is Confidence.PASS:
            return 0
        if signal.confidence.value not in self.config.execution.only_tiers:
            return 0
        # Never trade an empty ask book
        if signal.book_usd < 1.0:
            return 0
        if signal.book_usd < self.config.tiers.min_book_usd and signal.confidence is Confidence.HIGH:
            return 0

        max_usd = self.config.execution.max_position_usd
        remaining = max_usd - self.state.open_exposure_usd
        if remaining <= 0:
            return 0

        # Kelly-ish fractional: edge / (1 - entry) clipped hard
        edge = max(signal.edge_fraction, 0.0)
        entry = max(signal.kalshi_prob, 0.01)
        raw_kelly = edge / max(1.0 - entry, 0.05)
        frac = min(0.25, raw_kelly * 0.25)  # quarter-Kelly, capped
        if signal.confidence is Confidence.MEDIUM:
            frac *= 0.6
        elif signal.confidence is Confidence.LOW:
            frac *= 0.3

        budget = min(remaining, max_usd * frac, signal.book_usd * 0.5)
        contracts = int(budget / entry) if entry > 0 else 0
        contracts = max(0, min(contracts, self.config.execution.max_contracts_per_trade))
        return contracts

    def size_arb(self, edge: float, pair_cost: float, default_size: int) -> int:
        if self.state.halted or edge <= 0:
            return 0
        remaining = self.config.execution.max_position_usd - self.state.open_exposure_usd
        if remaining < pair_cost * default_size:
            return max(0, int(remaining / max(pair_cost, 0.01)))
        return default_size

    def register_fill(self, ticker: str, notional: float) -> None:
        self.state.open_exposure_usd += notional
        self.state.trades_today += 1
        self.state.positions[ticker] = self.state.positions.get(ticker, 0.0) + notional

    def register_pnl(self, pnl: float) -> None:
        self.state.realized_pnl += pnl
        if self.state.realized_pnl <= -abs(self.max_daily_loss):
            self.state.halted = True
            self.state.halt_reason = f"daily loss limit {self.max_daily_loss}"

    def release(self, ticker: str, notional: float) -> None:
        self.state.open_exposure_usd = max(0.0, self.state.open_exposure_usd - notional)
        self.state.positions[ticker] = max(0.0, self.state.positions.get(ticker, 0.0) - notional)