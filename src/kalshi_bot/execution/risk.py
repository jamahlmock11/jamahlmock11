"""Position sizing and risk limits."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from kalshi_bot.config import AppConfig
from kalshi_bot.domain import DecisionAction, DecisionResult
from kalshi_bot.models.probability import Confidence, EdgeSignal

HARD_MIN_EDGE = 0.20

# Legacy tier table kept for tests / backward compatibility only.
EDGE_TIER_SIZING: tuple[tuple[float, float], ...] = (
    (0.03, 5.0),
    (0.08, 9.0),
    (0.15, 14.0),
    (0.20, 20.0),
)


def quarter_kelly_bankroll_fraction(
    edge: float,
    *,
    kelly_fraction: float = 0.25,
    max_fraction: float = 0.25,
) -> float:
    """Quarter-Kelly bankroll fraction: edge / (1 - edge) * kelly_fraction."""
    edge = max(0.0, min(float(edge), 0.95))
    if edge <= 0.0:
        return 0.0
    raw = edge / max(1.0 - edge, 0.05) * kelly_fraction
    return min(max_fraction, raw)


def quarter_kelly_notional_usd(
    edge: float,
    bankroll_usd: float,
    *,
    kelly_fraction: float = 0.25,
    max_fraction: float = 0.25,
) -> float:
    """USD notional from quarter-Kelly fraction of bankroll."""
    return bankroll_usd * quarter_kelly_bankroll_fraction(
        edge,
        kelly_fraction=kelly_fraction,
        max_fraction=max_fraction,
    )


def kelly_notional_usd(edge: float, daily_cap: float) -> float:
    """Map edge to Kelly-tier notional, capped by daily risk budget."""
    edge = max(edge, 0.0)
    target = EDGE_TIER_SIZING[0][1]
    for threshold, notional in EDGE_TIER_SIZING:
        if edge >= threshold:
            target = notional
    return min(target, daily_cap)


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
    consecutive_losses: int = 0
    trades_by_contract: dict[str, int] = field(default_factory=dict)
    flips_by_contract: dict[str, int] = field(default_factory=dict)
    intent_ids: set[str] = field(default_factory=set)


class RiskManager:
    """Conservative live risk: one entry per ticker, cooldown, hard notional cap."""

    def __init__(
        self,
        config: AppConfig,
        max_daily_loss: float | None = None,
        cooldown_sec: float | None = None,
        max_trades_per_cycle: int = 1,
        max_per_ticker_usd: float | None = None,
        hard_min_edge: float | None = None,
    ):
        self.config = config
        self.hard_min_edge = (
            hard_min_edge if hard_min_edge is not None else HARD_MIN_EDGE
        )
        self.max_daily_loss = max_daily_loss or config.risk.max_daily_loss
        self.cooldown_sec = (
            config.risk.cooldown_seconds if cooldown_sec is None else cooldown_sec
        )
        self.max_trades_per_cycle = max_trades_per_cycle
        self.max_per_ticker_usd = (
            config.risk.max_contract_exposure
            if max_per_ticker_usd is None
            else max_per_ticker_usd
        )
        self.state = RiskState()

    @property
    def locked(self) -> bool:
        return self.state.halted

    def lock(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason

    def begin_cycle(self) -> None:
        self.state.trades_this_cycle = 0

    def size_mispricing(self, signal: EdgeSignal) -> int:
        if self.state.halted:
            return 0
        # Legacy strategy cannot bypass the system-wide hard edge invariant.
        if signal.edge_fraction + 1e-12 < self.hard_min_edge:
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

    def entry_allowed(
        self,
        *,
        ticker: str,
        edge: float,
        notional: float,
        intent_id: str,
    ) -> tuple[bool, str]:
        if edge + 1e-12 < self.hard_min_edge:
            return False, f"edge below non-overridable {self.hard_min_edge:.0%} minimum"
        if self.state.halted:
            return False, self.state.halt_reason or "risk lock active"
        if intent_id in self.state.intent_ids:
            return False, "duplicate order intent"
        if self.state.trades_this_cycle >= self.max_trades_per_cycle:
            return False, "cycle trade limit reached"
        if self.state.trades_by_contract.get(ticker, 0) >= self.config.risk.max_trades_per_contract:
            return False, "contract trade limit reached"
        existing = self.state.positions.get(ticker, 0.0)
        if existing + notional > self.max_per_ticker_usd + 1e-12:
            return False, "contract exposure limit reached"
        if self.state.open_exposure_usd + notional > self.config.risk.max_position_size + 1e-12:
            return False, "portfolio position limit reached"
        last = self.state.last_trade_ts.get(ticker, 0.0)
        if last and time.time() - last < self.cooldown_sec:
            return False, "contract cooldown active"
        return True, ""

    def kelly_contracts_for_entry(
        self,
        *,
        edge: float,
        executable_cost: float,
        size_multiplier: float = 1.0,
        ticker: str | None = None,
        min_edge: float | None = None,
    ) -> int:
        """Kelly-sized contract count for a new entry, capped by risk limits."""
        risk_cfg = self.config.risk
        if not self.config.risk.kelly_enabled or executable_cost <= 0:
            return 0
        min_price = self.config.strategy.min_entry_executable_cost
        if executable_cost + 1e-12 < min_price:
            return 0
        floor = self.hard_min_edge if min_edge is None else max(self.hard_min_edge, min_edge)
        if edge + 1e-12 < floor:
            return 0

        bankroll = risk_cfg.kelly_bankroll_usd or risk_cfg.max_position_size
        daily_room = max(0.0, abs(self.max_daily_loss) + self.state.realized_pnl)
        portfolio_room = max(0.0, risk_cfg.max_position_size - self.state.open_exposure_usd)
        existing = self.state.positions.get(ticker or "", 0.0)
        per_ticker_room = max(0.0, self.max_per_ticker_usd - existing)

        kelly_budget = quarter_kelly_notional_usd(
            edge,
            bankroll,
            kelly_fraction=risk_cfg.kelly_fraction,
            max_fraction=risk_cfg.kelly_max_fraction,
        )
        size_mult = max(0.0, min(1.0, size_multiplier))
        available_usd = max(
            0.0,
            min(portfolio_room, per_ticker_room, daily_room, kelly_budget * size_mult),
        )
        contracts = int(available_usd / executable_cost)
        min_notional = self.config.execution.min_trade_notional_usd
        if min_notional > 0:
            contracts = max(contracts, math.ceil(min_notional / executable_cost))
        return max(
            0,
            min(contracts, self.config.execution.max_contracts_per_trade),
        )

    def size_decision(self, decision: DecisionResult) -> int:
        if decision.action not in {DecisionAction.BUY_UP, DecisionAction.BUY_DOWN}:
            return 0
        if decision.edge is None or decision.executable_cost is None:
            return 0
        min_edge = self.hard_min_edge
        if decision.required_edge is not None:
            min_edge = max(self.hard_min_edge, float(decision.required_edge))
        if decision.edge + 1e-12 < min_edge or decision.executable_cost <= 0:
            return 0
        size_mult = max(0.0, min(1.0, decision.size_multiplier))
        if self.config.risk.kelly_enabled:
            kelly_qty = self.kelly_contracts_for_entry(
                edge=decision.edge,
                executable_cost=decision.executable_cost,
                size_multiplier=size_mult,
                min_edge=min_edge,
            )
            if kelly_qty > 0:
                return kelly_qty
        daily_room = max(0.0, abs(self.max_daily_loss) + self.state.realized_pnl)
        kelly_budget = kelly_notional_usd(decision.edge, daily_room)
        available_usd = max(
            0.0,
            min(
                self.config.risk.max_position_size - self.state.open_exposure_usd,
                self.max_per_ticker_usd,
                kelly_budget,
            ),
        )
        affordable = int(available_usd / decision.executable_cost)
        requested = max(1, int(decision.quantity * size_mult))
        min_notional = self.config.execution.min_trade_notional_usd
        if min_notional > 0:
            requested = max(
                requested,
                math.ceil(min_notional / decision.executable_cost),
            )
        return max(
            0,
            min(requested, affordable, self.config.execution.max_contracts_per_trade),
        )

    def register_intent(self, intent_id: str) -> None:
        self.state.intent_ids.add(intent_id)

    def size_arb(self, edge: float, pair_cost: float, default_size: int) -> int:
        # Cross-venue pair cost has basis/execution risk and is not a calibrated
        # terminal probability edge. It cannot satisfy the mandated entry rule.
        return 0

    def size_alt_signal(self, edge: float, limit_price: float) -> int:
        if self.state.halted or limit_price <= 0:
            return 0
        if self.state.trades_this_cycle >= self.max_trades_per_cycle:
            return 0
        daily_room = max(0.0, abs(self.max_daily_loss) + self.state.realized_pnl)
        bankroll = self.config.risk.kelly_bankroll_usd or self.config.risk.max_position_size
        if self.config.risk.kelly_enabled:
            budget = min(
                self.config.risk.max_position_size - self.state.open_exposure_usd,
                self.config.risk.max_contract_exposure,
                daily_room,
                quarter_kelly_notional_usd(
                    max(edge, 0.0),
                    bankroll,
                    kelly_fraction=self.config.risk.kelly_fraction,
                    max_fraction=self.config.risk.kelly_max_fraction,
                ),
            )
        else:
            budget = min(
                self.config.risk.max_position_size - self.state.open_exposure_usd,
                self.config.risk.max_contract_exposure,
                kelly_notional_usd(max(edge, 0.0), daily_room),
            )
        return max(
            0,
            min(
                int(budget / limit_price),
                self.config.execution.max_contracts_per_trade,
            ),
        )

    def alt_entry_allowed(
        self,
        *,
        ticker: str,
        edge: float,
        notional: float,
        intent_id: str,
        min_edge: float,
    ) -> tuple[bool, str]:
        if edge + 1e-12 < min_edge:
            return False, f"edge below strategy minimum {min_edge:.0%}"
        if self.state.halted:
            return False, self.state.halt_reason or "risk lock active"
        if intent_id in self.state.intent_ids:
            return False, "duplicate order intent"
        if self.state.trades_this_cycle >= self.max_trades_per_cycle:
            return False, "cycle trade limit reached"
        if self.state.trades_by_contract.get(ticker, 0) >= self.config.risk.max_trades_per_contract:
            return False, "contract trade limit reached"
        existing = self.state.positions.get(ticker, 0.0)
        if existing + notional > self.max_per_ticker_usd + 1e-12:
            return False, "contract exposure limit reached"
        if self.state.open_exposure_usd + notional > self.config.risk.max_position_size + 1e-12:
            return False, "portfolio position limit reached"
        last = self.state.last_trade_ts.get(ticker, 0.0)
        if last and time.time() - last < self.cooldown_sec:
            return False, "contract cooldown active"
        return True, ""

    def register_fill(self, ticker: str, notional: float) -> None:
        self.state.open_exposure_usd += notional
        self.state.trades_today += 1
        self.state.trades_this_cycle += 1
        self.state.positions[ticker] = self.state.positions.get(ticker, 0.0) + notional
        self.state.last_trade_ts[ticker] = time.time()
        self.state.trades_by_contract[ticker] = (
            self.state.trades_by_contract.get(ticker, 0) + 1
        )

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
        if pnl < 0:
            self.state.consecutive_losses += 1
        elif pnl > 0:
            self.state.consecutive_losses = 0
        if self.state.realized_pnl <= -abs(self.max_daily_loss):
            self.lock(f"daily loss limit {self.max_daily_loss}")
        elif self.state.consecutive_losses >= self.config.risk.max_consecutive_losses:
            self.lock(
                f"consecutive loss limit {self.config.risk.max_consecutive_losses}"
            )

    def release(self, ticker: str, notional: float) -> None:
        self.state.open_exposure_usd = max(0.0, self.state.open_exposure_usd - notional)
        self.state.positions[ticker] = max(0.0, self.state.positions.get(ticker, 0.0) - notional)