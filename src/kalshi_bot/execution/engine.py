"""Order execution — dry-run by default; live Kalshi when credentials present."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kalshi_bot.config import AppConfig
from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    DecisionResult,
    MarketSnapshot,
)
from kalshi_bot.execution.position_manager import (
    PositionManager,
    PositionManagerError,
)
from kalshi_bot.execution.risk import RiskManager
from kalshi_bot.journal import TradeJournal
from kalshi_bot.models.probability import EdgeSignal, Side
from kalshi_bot.strategies.cross_venue_arb import ArbOpportunity
from kalshi_bot.venues.kalshi import KalshiClient

logger = logging.getLogger(__name__)


@dataclass
class ExecutionReport:
    ok: bool
    dry_run: bool
    strategy: str
    detail: str
    payload: dict[str, Any]
    trade_id: str | None = None
    realized_pnl: float | None = None


class ExecutionEngine:
    def __init__(
        self,
        kalshi: KalshiClient,
        risk: RiskManager,
        config: AppConfig,
        journal: TradeJournal | None = None,
        positions: PositionManager | None = None,
    ):
        self.kalshi = kalshi
        self.risk = risk
        self.config = config
        self.journal = journal
        self.positions = positions or PositionManager(mode="paper" if self.dry_run else "live")

    @property
    def dry_run(self) -> bool:
        return self.config.execution.dry_run or not self.kalshi.authenticated

    def _required_entry_edge(self, decision: DecisionResult) -> float:
        """Minimum edge for live entry; hour bot can use tiered required_edge."""
        floor = self.risk.hard_min_edge
        if decision.required_edge is not None:
            return max(floor, float(decision.required_edge))
        return floor

    def _persist_trade(self, **kwargs: Any) -> str | None:
        if not self.journal:
            return None
        return self.journal.log_trade(**kwargs)

    def execute_mispricing(self, signal: EdgeSignal) -> ExecutionReport | None:
        size = self.risk.size_mispricing(signal)
        if size <= 0:
            return None
        price_cents = max(1, min(99, int(round(signal.kalshi_prob * 100))))
        price = price_cents / 100.0
        side = "yes" if signal.side is Side.YES else "no"
        client_id = f"misprice-{uuid.uuid4().hex[:12]}"
        notional = size * price

        if self.dry_run:
            self.risk.register_fill(signal.ticker, notional)
            detail = (
                f"[DRY-RUN] BUY {size} {side.upper()} {signal.ticker} "
                f"@{price_cents}¢ | {signal.confidence.value} edge={signal.edge_pp:.1f}pp"
            )
            logger.info(detail)
            payload = {
                "ticker": signal.ticker,
                "side": side,
                "count": size,
                "price_cents": price_cents,
                "edge_pp": signal.edge_pp,
                "confidence": signal.confidence.value,
            }
            trade_id = self._persist_trade(
                strategy="mispricing",
                ticker=signal.ticker,
                side=side,
                count=size,
                price=price,
                notional=notional,
                edge=signal.edge_pp,
                confidence=signal.confidence.value,
                dry_run=True,
                ok=True,
                detail=detail,
                payload=payload,
            )
            return ExecutionReport(True, True, "mispricing", detail, payload, trade_id)

        try:
            resp = self.kalshi.create_order(
                ticker=signal.ticker,
                side=side,
                action="buy",
                count=size,
                yes_price=price_cents if side == "yes" else None,
                no_price=price_cents if side == "no" else None,
                client_order_id=client_id,
            )
            self.risk.register_fill(signal.ticker, notional)
            detail = f"[LIVE] order placed {signal.ticker} {side} x{size} @{price_cents}¢"
            logger.info("%s → %s", detail, resp)
            payload = {
                "response": resp,
                "ticker": signal.ticker,
                "side": side,
                "count": size,
                "price_cents": price_cents,
                "client_order_id": client_id,
            }
            trade_id = self._persist_trade(
                strategy="mispricing",
                ticker=signal.ticker,
                side=side,
                count=size,
                price=price,
                notional=notional,
                edge=signal.edge_pp,
                confidence=signal.confidence.value,
                dry_run=False,
                ok=True,
                detail=detail,
                payload=payload,
            )
            return ExecutionReport(True, False, "mispricing", detail, payload, trade_id)
        except Exception as exc:
            detail = f"[LIVE] order failed {signal.ticker}: {exc}"
            logger.error(detail)
            trade_id = self._persist_trade(
                strategy="mispricing",
                ticker=signal.ticker,
                side=side,
                count=size,
                price=price,
                notional=notional,
                edge=signal.edge_pp,
                confidence=signal.confidence.value,
                dry_run=False,
                ok=False,
                detail=detail,
                payload={"error": str(exc)},
            )
            return ExecutionReport(False, False, "mispricing", detail, {"error": str(exc)}, trade_id)

    def execute_decision(
        self,
        market: MarketSnapshot,
        decision: DecisionResult,
        *,
        timestamp: datetime | None = None,
        intent_id: str | None = None,
        benchmark: BenchmarkQuote | None = None,
    ) -> ExecutionReport | None:
        """Execute one already safety-gated decision with a final invariant check."""
        if decision.action in {DecisionAction.NO_TRADE, DecisionAction.HOLD}:
            return None
        observed_at = timestamp or datetime.now(timezone.utc)
        side = decision.selected_side
        if side is None:
            return ExecutionReport(
                False,
                self.dry_run,
                "forecast",
                "decision has no selected side",
                {},
            )
        if market.status.lower() not in {"open", "active"} or not market.valid:
            return ExecutionReport(False, self.dry_run, "forecast", "final market validation failed", {})
        if market.expiration <= observed_at:
            return ExecutionReport(False, self.dry_run, "forecast", "market is expired", {})

        intent = intent_id or (
            f"forecast-{market.ticker}-{decision.action.value.lower()}-"
            f"{int(observed_at.timestamp() * 1000)}"
        )
        side_text = "yes" if side is ContractSide.YES else "no"

        if decision.action in {DecisionAction.BUY_UP, DecisionAction.BUY_DOWN}:
            if not self.dry_run and (
                benchmark is None
                or not benchmark.primary
                or benchmark.is_proxy
                or benchmark.replay
            ):
                return ExecutionReport(
                    False,
                    False,
                    "forecast",
                    "LIVE entry requires official primary BRTI",
                    {},
                )
            if (
                decision.gate_failures
                or decision.edge is None
                or decision.edge + 1e-12 < self._required_entry_edge(decision)
                or decision.execution is None
                or not decision.execution.fully_filled
            ):
                return ExecutionReport(
                    False,
                    self.dry_run,
                    "forecast",
                    "final edge/data/execution validation failed",
                    {"edge": decision.edge},
                )
            size = self.risk.size_decision(decision)
            if size <= 0:
                return None
            order_price = min(
                0.99,
                decision.execution.average_price
                + decision.execution.slippage_per_contract,
            )
            notional = size * decision.execution.executable_cost
            allowed, reason = self.risk.entry_allowed(
                ticker=market.ticker,
                edge=decision.edge,
                notional=notional,
                intent_id=intent,
            )
            if not allowed:
                return ExecutionReport(False, self.dry_run, "forecast", reason, {})
            price_cents = max(1, min(99, int(round(order_price * 100))))
            payload: dict[str, Any] = {
                "ticker": market.ticker,
                "action": decision.action.value,
                "side": side_text,
                "count": size,
                "price_cents": price_cents,
                "model_probability": decision.predicted_probability,
                "executable_cost": decision.executable_cost,
                "edge": decision.edge,
                "client_order_id": intent,
            }
            try:
                if not self.dry_run:
                    payload["response"] = self.kalshi.create_order(
                        ticker=market.ticker,
                        side=side_text,
                        action="buy",
                        count=size,
                        yes_price=price_cents if side is ContractSide.YES else None,
                        no_price=price_cents if side is ContractSide.NO else None,
                        client_order_id=intent,
                    )
                self.positions.enter(
                    intent_id=intent,
                    contract=market.ticker,
                    side=side,
                    quantity=size,
                    price=order_price,
                    fee=decision.execution.fee_per_contract * size,
                    timestamp=observed_at,
                )
                self.risk.register_intent(intent)
                self.risk.register_fill(market.ticker, notional)
                detail = (
                    f"{'[PAPER]' if self.dry_run else '[LIVE]'} "
                    f"{decision.action.value} {size} {market.ticker} @{price_cents}¢ "
                    f"edge={decision.edge:.1%}"
                )
                trade_id = self._persist_trade(
                    strategy="forecast",
                    ticker=market.ticker,
                    side=side_text,
                    count=size,
                    price=order_price,
                    notional=notional,
                    edge=decision.edge * 100,
                    confidence="ENSEMBLE",
                    dry_run=self.dry_run,
                    ok=True,
                    detail=detail,
                    payload=payload,
                )
                return ExecutionReport(True, self.dry_run, "forecast", detail, payload, trade_id)
            except Exception as exc:
                detail = f"order failed safely: {exc}"
                return ExecutionReport(
                    False,
                    self.dry_run,
                    "forecast",
                    detail,
                    {"error": str(exc), **payload},
                )

        if decision.action is DecisionAction.EXIT:
            position = self.positions.position(market.ticker)
            if position is None or position.side is not side:
                return ExecutionReport(False, self.dry_run, "forecast", "no matching position to exit", {})
            bids = market.orderbook.levels(side, asks=False)
            if not bids or sum(level.size for level in bids) + 1e-12 < position.quantity:
                return ExecutionReport(False, self.dry_run, "forecast", "insufficient exit liquidity", {})
            remaining = position.quantity
            proceeds = 0.0
            for level in bids:
                filled = min(remaining, level.size)
                proceeds += filled * level.price
                remaining -= filled
                if remaining <= 1e-12:
                    break
            exit_price = proceeds / position.quantity
            price_cents = max(1, min(99, int(round(exit_price * 100))))
            payload = {
                "ticker": market.ticker,
                "action": "EXIT",
                "side": side_text,
                "count": position.quantity,
                "price_cents": price_cents,
                "client_order_id": intent,
            }
            try:
                if not self.dry_run:
                    payload["response"] = self.kalshi.create_order(
                        ticker=market.ticker,
                        side=side_text,
                        action="sell",
                        count=int(position.quantity),
                        yes_price=price_cents if side is ContractSide.YES else None,
                        no_price=price_cents if side is ContractSide.NO else None,
                        client_order_id=intent,
                    )
                exit_record = self.positions.exit(
                    intent_id=intent,
                    contract=market.ticker,
                    price=exit_price,
                    timestamp=observed_at,
                    reason=decision.reason,
                )
                self.risk.register_intent(intent)
                self.risk.release(
                    market.ticker,
                    position.quantity * position.entry_price,
                )
                self.risk.register_pnl(exit_record.realized_pnl)
                detail = (
                    f"{'[PAPER]' if self.dry_run else '[LIVE]'} EXIT "
                    f"{position.quantity:g} {side.value} {market.ticker} @{price_cents}¢ "
                    f"pnl=${exit_record.realized_pnl:.2f}"
                )
                trade_id = self._persist_trade(
                    strategy="forecast_exit",
                    ticker=market.ticker,
                    side=side_text,
                    count=position.quantity,
                    price=exit_price,
                    notional=position.quantity * exit_price,
                    edge=None,
                    confidence="EXIT",
                    dry_run=self.dry_run,
                    ok=True,
                    detail=detail,
                    payload=payload,
                )
                return ExecutionReport(
                    True,
                    self.dry_run,
                    "forecast_exit",
                    detail,
                    payload,
                    trade_id,
                    exit_record.realized_pnl,
                )
            except (PositionManagerError, Exception) as exc:
                return ExecutionReport(
                    False,
                    self.dry_run,
                    "forecast_exit",
                    f"exit failed safely: {exc}",
                    {"error": str(exc), **payload},
                )

    def execute_arb(self, opp: ArbOpportunity) -> ExecutionReport | None:
        size = self.risk.size_arb(opp.edge, opp.pair_cost, self.config.cross_venue.order_size)
        if size <= 0:
            return None
        detail = (
            f"{'[DRY-RUN] ' if self.dry_run else ''}"
            f"ARB size={size} cost={opp.pair_cost:.3f} edge={opp.edge:.3f} | {opp.reason}"
        )
        logger.info(detail)
        notional = size * opp.pair_cost
        self.risk.register_fill(opp.kalshi_ticker, notional)
        ok = True

        if not self.dry_run and self.kalshi.authenticated:
            side = opp.kalshi_leg.side.lower()
            price_cents = max(1, min(99, int(round(opp.kalshi_leg.price * 100))))
            try:
                self.kalshi.create_order(
                    ticker=opp.kalshi_ticker,
                    side="yes" if side == "yes" else "no",
                    action="buy",
                    count=size,
                    yes_price=price_cents if side == "yes" else None,
                    no_price=price_cents if side == "no" else None,
                    client_order_id=f"arb-{uuid.uuid4().hex[:12]}",
                )
            except Exception as exc:
                detail = f"Kalshi leg failed: {exc}"
                ok = False
                trade_id = self._persist_trade(
                    strategy="cross_venue_arb",
                    ticker=opp.kalshi_ticker,
                    side=opp.kalshi_leg.side,
                    count=size,
                    price=opp.pair_cost,
                    notional=notional,
                    edge=opp.edge * 100,
                    confidence="ARB",
                    dry_run=False,
                    ok=False,
                    detail=detail,
                    payload={"error": str(exc)},
                )
                return ExecutionReport(False, False, "cross_venue_arb", detail, {"error": str(exc)}, trade_id)
            detail += " | WARNING: Polymarket leg not auto-executed — complete manually or wire py-clob-client"

        payload = {
            "size": size,
            "pair_cost": opp.pair_cost,
            "edge": opp.edge,
            "kalshi": opp.kalshi_leg.__dict__,
            "poly": opp.poly_leg.__dict__,
            "ts": time.time(),
        }
        trade_id = self._persist_trade(
            strategy="cross_venue_arb",
            ticker=opp.kalshi_ticker,
            side=opp.kalshi_leg.side,
            count=size,
            price=opp.pair_cost,
            notional=notional,
            edge=opp.edge * 100,
            confidence="ARB",
            dry_run=self.dry_run,
            ok=ok,
            detail=detail,
            payload=payload,
        )
        return ExecutionReport(ok, self.dry_run, "cross_venue_arb", detail, payload, trade_id)

    def execute_alt_signal(self, signal) -> ExecutionReport | None:
        """Execute spot-lag, skew, or mean-reversion signals with slippage logging."""
        from kalshi_bot.strategies.alt_signal import AltTradeSignal

        if not isinstance(signal, AltTradeSignal):
            return None
        if signal.quantity <= 0:
            return None

        size = self.risk.size_alt_signal(signal.edge, signal.limit_price)
        if size <= 0:
            return None

        side_text = "yes" if signal.side.value == "YES" else "no"
        price_cents = max(1, min(99, int(round(signal.limit_price * 100))))
        notional = size * signal.limit_price
        min_edge = {
            "spot_lag": self.config.spot_lag.min_edge,
            "orderbook_skew": self.config.orderbook_skew.min_edge,
            "mean_reversion": 0.01,
        }.get(signal.strategy, self.risk.hard_min_edge)
        allowed, reason = self.risk.alt_entry_allowed(
            ticker=signal.ticker,
            edge=signal.edge,
            notional=notional,
            intent_id=signal.intent_id,
            min_edge=min_edge,
        )
        if not allowed:
            return ExecutionReport(False, self.dry_run, signal.strategy, reason, {})

        payload: dict[str, Any] = {
            "ticker": signal.ticker,
            "side": side_text,
            "action": signal.action,
            "count": size,
            "price_cents": price_cents,
            "edge": signal.edge,
            "time_in_force": signal.time_in_force,
            "rationale": signal.rationale,
            "client_order_id": signal.intent_id,
        }
        detail = (
            f"[{'DRY-RUN' if self.dry_run else 'LIVE'}] {signal.strategy.upper()} "
            f"{signal.action.upper()} {size} {side_text.upper()} {signal.ticker} "
            f"@{price_cents}¢ | {signal.reason}"
        )
        logger.info("%s — %s", detail, signal.rationale)

        if self.dry_run:
            if signal.action == "buy":
                self.risk.register_fill(signal.ticker, notional)
            self.risk.register_intent(signal.intent_id)
            trade_id = self._persist_trade(
                strategy=signal.strategy,
                ticker=signal.ticker,
                side=side_text,
                count=size,
                price=signal.limit_price,
                notional=notional,
                edge=signal.edge * 100,
                confidence=signal.strategy,
                dry_run=True,
                ok=True,
                detail=detail,
                payload=payload,
            )
            return ExecutionReport(True, True, signal.strategy, detail, payload, trade_id)

        try:
            if signal.action == "sell":
                resp = self.kalshi.create_order(
                    ticker=signal.ticker,
                    side=side_text,
                    action="sell",
                    count=size,
                    yes_price=price_cents if side_text == "yes" else None,
                    no_price=price_cents if side_text == "no" else None,
                    client_order_id=signal.intent_id,
                    time_in_force=signal.time_in_force,
                )
            else:
                resp = self.kalshi.create_order(
                    ticker=signal.ticker,
                    side=side_text,
                    action="buy",
                    count=size,
                    yes_price=price_cents if side_text == "yes" else None,
                    no_price=price_cents if side_text == "no" else None,
                    client_order_id=signal.intent_id,
                    time_in_force=signal.time_in_force,
                )
            payload["response"] = resp
            if signal.action == "buy":
                self.risk.register_fill(signal.ticker, notional)
            self.risk.register_intent(signal.intent_id)
            trade_id = self._persist_trade(
                strategy=signal.strategy,
                ticker=signal.ticker,
                side=side_text,
                count=size,
                price=signal.limit_price,
                notional=notional,
                edge=signal.edge * 100,
                confidence=signal.strategy,
                dry_run=False,
                ok=True,
                detail=detail,
                payload=payload,
            )
            return ExecutionReport(True, False, signal.strategy, detail, payload, trade_id)
        except Exception as exc:
            detail = f"[LIVE] {signal.strategy} failed {signal.ticker}: {exc}"
            logger.error(detail)
            return ExecutionReport(
                False, False, signal.strategy, detail, {"error": str(exc)}
            )