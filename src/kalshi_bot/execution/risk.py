"""Position sizing and risk limits."""

from __future__ import annotations

import time
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
    last_trade_ts: dict[str, float] = field(default_factory=dict)
    trades_this_cycle: int = 0


class RiskManager:
    """Conservative live risk: one entry per ticker, cooldown, hard notional cap."""

    def __init__(
        self,
        config: AppConfig,
        max_daily_loss: float = 100.0,
        cooldown_sec: float = 900.0,
        max_trades_per_cycle: int = 1,
        max_per_ticker_usd: float = 2.0,
    ):
        self.config = config
        self.max_daily_loss = max_daily_loss
        self.cooldown_sec = cooldown_sec
        self.max_trades_per_cycle = max_trades_per_cycle
        self.max_per_ticker_usd = max_per_ticker_usd
        self.state = RiskState()

    def begin_cycle(self) -> None:
        self.state.trades_this_cycle = 0

    def size_mispricing(self, signal: EdgeSignal) -> int:
        if self.state.halted:
            return 0
        if signal.confidence is Confidence.PASS:
            return 0
        if signal.confidence.value not in self.config.execution.only_tiers:
            return 0
        if signal.book_usd < 1.0:
            return 0
        if signal.book_usd < self.config.tiers.min_book_usd and signal.confidence is Confidence.HIGH:
            return 0
        if self.state.trades_this_cycle >= self.max_trades_per_cycle:
            return 0

        # Already in this market — do not pyramid
        existing = self.state.positions.get(signal.ticker, 0.0)
        if existing >= self.max_per_ticker_usd:
            return 0
        last = self.state.last_trade_ts.get(signal.ticker, 0.0)
        if last and (time.time() - last) < self.cooldown_sec:
            return 0

        max_usd = self.config.execution.max_position_usd
        remaining = max_usd - self.state.open_exposure_usd
        if remaining <= 0.05:
            return 0

        edge = max(signal.edge_fraction, 0.0)
        entry = max(signal.kalshi_prob, 0.01)
        raw_kelly = edge / max(1.0 - entry, 0.05)
        frac = min(0.25, raw_kelly * 0.25)
        if signal.confidence is Confidence.MEDIUM:
            frac *= 0.6
        elif signal.confidence is Confidence.LOW:
            frac *= 0.3

        per_ticker_room = max(0.0, self.max_per_ticker_usd - existing)
        budget = min(remaining, max_usd * frac, signal.book_usd * 0.25, per_ticker_room)
        contracts = int(budget / entry) if entry > 0 else 0
        contracts = max(0, min(contracts, self.config.execution.max_contracts_per_trade))
        return contracts

    def size_arb(self, edge: float, pair_cost: float, default_size: int) -> int:
        if self.state.halted or edge <= 0:
            return 0
        if self.state.trades_this_cycle >= self.max_trades_per_cycle:
            return 0
        remaining = self.config.execution.max_position_usd - self.state.open_exposure_usd
        if remaining < pair_cost * default_size:
            return max(0, int(remaining / max(pair_cost, 0.01)))
        return default_size

    def register_fill(self, ticker: str, notional: float) -> None:
        self.state.open_exposure_usd += notional
        self.state.trades_today += 1
        self.state.trades_this_cycle += 1
        self.state.positions[ticker] = self.state.positions.get(ticker, 0.0) + notional
        self.state.last_trade_ts[ticker] = time.time()

    def seed_from_positions(self, market_positions: list[dict]) -> None:
        """Hydrate exposure from Kalshi portfolio so restarts don't double up."""
        total = 0.0
        for p in market_positions:
            ticker = p.get("ticker") or ""
            exposure = float(p.get("market_exposure_dollars") or 0)
            if not ticker:
                continue
            self.state.positions[ticker] = exposure
            total += exposure
            self.state.last_trade_ts[ticker] = time.time()
        self.state.open_exposure_usd = total

    def register_pnl(self, pnl: float) -> None:
        self.state.realized_pnl += pnl
        if self.state.realized_pnl <= -abs(self.max_daily_loss):
            self.state.halted = True
            self.state.halt_reason = f"daily loss limit {self.max_daily_loss}"

    def release(self, ticker: str, notional: float) -> None:
        self.state.open_exposure_usd = max(0.0, self.state.open_exposure_usd - notional)
        self.state.positions[ticker] = max(0.0, self.state.positions.get(ticker, 0.0) - notional)