"""Order execution — dry-run by default; live Kalshi when credentials present."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from kalshi_bot.config import AppConfig
from kalshi_bot.execution.risk import RiskManager
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


class ExecutionEngine:
    def __init__(self, kalshi: KalshiClient, risk: RiskManager, config: AppConfig):
        self.kalshi = kalshi
        self.risk = risk
        self.config = config

    @property
    def dry_run(self) -> bool:
        return self.config.execution.dry_run or not self.kalshi.authenticated

    def execute_mispricing(self, signal: EdgeSignal) -> ExecutionReport | None:
        size = self.risk.size_mispricing(signal)
        if size <= 0:
            return None
        price_cents = max(1, min(99, int(round(signal.kalshi_prob * 100))))
        side = "yes" if signal.side is Side.YES else "no"
        client_id = f"misprice-{uuid.uuid4().hex[:12]}"
        notional = size * signal.kalshi_prob

        if self.dry_run:
            self.risk.register_fill(signal.ticker, notional)
            detail = (
                f"[DRY-RUN] BUY {size} {side.upper()} {signal.ticker} "
                f"@{price_cents}¢ | {signal.confidence.value} edge={signal.edge_pp:.1f}pp"
            )
            logger.info(detail)
            return ExecutionReport(
                ok=True,
                dry_run=True,
                strategy="mispricing",
                detail=detail,
                payload={
                    "ticker": signal.ticker,
                    "side": side,
                    "count": size,
                    "price_cents": price_cents,
                    "edge_pp": signal.edge_pp,
                    "confidence": signal.confidence.value,
                },
            )

        body: dict[str, Any] = {
            "ticker": signal.ticker,
            "side": side,
            "action": "buy",
            "count": size,
            "type": "limit",
            "client_order_id": client_id,
        }
        if side == "yes":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents
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
            return ExecutionReport(True, False, "mispricing", detail, {"response": resp, **body})
        except Exception as exc:
            detail = f"[LIVE] order failed {signal.ticker}: {exc}"
            logger.error(detail)
            return ExecutionReport(False, False, "mispricing", detail, {"error": str(exc)})

    def execute_arb(self, opp: ArbOpportunity) -> ExecutionReport | None:
        size = self.risk.size_arb(opp.edge, opp.pair_cost, self.config.cross_venue.order_size)
        if size <= 0:
            return None
        # Polymarket live execution requires wallet keys; always dry-run poly leg
        # unless extended later. Kalshi leg follows dry_run flag.
        detail = (
            f"{'[DRY-RUN] ' if self.dry_run else ''}"
            f"ARB size={size} cost={opp.pair_cost:.3f} edge={opp.edge:.3f} | {opp.reason}"
        )
        logger.info(detail)
        notional = size * opp.pair_cost
        self.risk.register_fill(opp.kalshi_ticker, notional)

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
                return ExecutionReport(
                    False, False, "cross_venue_arb", f"Kalshi leg failed: {exc}", {"error": str(exc)}
                )
            detail += " | WARNING: Polymarket leg not auto-executed — complete manually or wire py-clob-client"

        return ExecutionReport(
            ok=True,
            dry_run=self.dry_run,
            strategy="cross_venue_arb",
            detail=detail,
            payload={
                "size": size,
                "pair_cost": opp.pair_cost,
                "edge": opp.edge,
                "kalshi": opp.kalshi_leg.__dict__,
                "poly": opp.poly_leg.__dict__,
                "ts": time.time(),
            },
        )