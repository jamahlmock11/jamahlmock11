from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshi_btc_edge.models import TradeIntent

log = logging.getLogger(__name__)


@dataclass
class Fill:
    intent: TradeIntent
    filled_at: datetime
    notional: float


@dataclass
class PaperBroker:
    fills: list[Fill] = field(default_factory=list)

    @property
    def open_notional(self) -> float:
        return sum(f.notional for f in self.fills)

    def execute(self, intent: TradeIntent) -> Fill:
        intent.paper = True
        fill = Fill(
            intent=intent,
            filled_at=datetime.now(timezone.utc),
            notional=intent.contracts * intent.limit_price,
        )
        self.fills.append(fill)
        log.info(
            "PAPER FILL %s %s x%d @ %.4f conf=%s edge=%+.1fpp",
            intent.side.value,
            intent.market_ticker,
            intent.contracts,
            intent.limit_price,
            intent.confidence.value,
            intent.edge_pp,
        )
        return fill
