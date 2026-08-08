"""Order execution — dry-run by default; live Kalshi when credentials present."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from kalshi_bot.config import AppConfig
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


class ExecutionEngine:
    def __init__(
        self,
        kalshi: KalshiClient,
        risk: RiskManager,
        config: AppConfig,
        journal: TradeJournal | None = None,
    ):
        self.kalshi = kalshi
        self.risk = risk
        self.config = config
        self.journal = journal

    @property
    def dry_run(self) -> bool:
        return self.config.execution.dry_run or not self.kalshi.authenticated

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