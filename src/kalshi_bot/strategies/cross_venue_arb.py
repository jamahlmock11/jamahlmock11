"""Cross-venue arbitrage: Kalshi ↔ Polymarket BTC 15-minute markets.

Risk-free when:
  Kalshi UP (YES) ask + Polymarket DOWN ask < $1.00
  or Kalshi DOWN (NO) ask + Polymarket UP ask < $1.00
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kalshi_bot.config import CrossVenueConfig
from kalshi_bot.venues.kalshi import KalshiClient, KalshiMarket
from kalshi_bot.venues.polymarket import PolymarketClient, PolyMarket

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArbLeg:
    venue: str
    ticker_or_token: str
    side: str  # YES/NO or UP/DOWN
    price: float


@dataclass(frozen=True)
class ArbOpportunity:
    pair_cost: float
    edge: float  # 1 - pair_cost
    kalshi_leg: ArbLeg
    poly_leg: ArbLeg
    kalshi_ticker: str
    poly_slug: str
    reason: str

    @property
    def is_risk_free(self) -> bool:
        return self.pair_cost < 1.0


class CrossVenueArbScanner:
    def __init__(
        self,
        kalshi: KalshiClient,
        poly: PolymarketClient,
        config: CrossVenueConfig,
    ):
        self.kalshi = kalshi
        self.poly = poly
        self.config = config

    def scan(self) -> list[ArbOpportunity]:
        if not self.config.enabled:
            return []
        poly_mkt = self.poly.get_btc_15m()
        if not poly_mkt:
            logger.warning("No active Polymarket BTC 15m market")
            return []

        try:
            kalshi_mkts = self.kalshi.get_markets("KXBTC15M", status="open", limit=20)
        except Exception as exc:
            logger.error("Kalshi 15m fetch failed: %s", exc)
            return []

        # Match by closest close time to poly end
        live = [m for m in kalshi_mkts if m.seconds_to_close > 20]
        if not live:
            return []
        matched = min(
            live,
            key=lambda m: abs(m.close_time.timestamp() - poly_mkt.end_ts),
        )
        return self._evaluate(matched, poly_mkt)

    def _evaluate(self, k: KalshiMarket, p: PolyMarket) -> list[ArbOpportunity]:
        opps: list[ArbOpportunity] = []
        # Path A: buy Kalshi YES (UP) + Polymarket DOWN
        cost_a = k.yes_ask + p.down_price
        if cost_a <= self.config.max_pair_cost and (1.0 - cost_a) >= self.config.min_edge_usd:
            opps.append(
                ArbOpportunity(
                    pair_cost=cost_a,
                    edge=1.0 - cost_a,
                    kalshi_leg=ArbLeg("kalshi", k.ticker, "YES", k.yes_ask),
                    poly_leg=ArbLeg("polymarket", p.down_token_id, "DOWN", p.down_price),
                    kalshi_ticker=k.ticker,
                    poly_slug=p.slug,
                    reason=(
                        f"Kalshi UP@{k.yes_ask:.3f} + Poly DOWN@{p.down_price:.3f} "
                        f"= {cost_a:.3f} (edge {1-cost_a:.3f})"
                    ),
                )
            )
        # Path B: buy Kalshi NO (DOWN) + Polymarket UP
        cost_b = k.no_ask + p.up_price
        if cost_b <= self.config.max_pair_cost and (1.0 - cost_b) >= self.config.min_edge_usd:
            opps.append(
                ArbOpportunity(
                    pair_cost=cost_b,
                    edge=1.0 - cost_b,
                    kalshi_leg=ArbLeg("kalshi", k.ticker, "NO", k.no_ask),
                    poly_leg=ArbLeg("polymarket", p.up_token_id, "UP", p.up_price),
                    kalshi_ticker=k.ticker,
                    poly_slug=p.slug,
                    reason=(
                        f"Kalshi DOWN@{k.no_ask:.3f} + Poly UP@{p.up_price:.3f} "
                        f"= {cost_b:.3f} (edge {1-cost_b:.3f})"
                    ),
                )
            )
        opps.sort(key=lambda o: o.edge, reverse=True)
        return opps