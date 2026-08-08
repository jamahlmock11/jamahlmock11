from __future__ import annotations

import logging
import os
import time
from typing import Optional

from kalshi_btc_edge.clients.kalshi import KalshiClient
from kalshi_btc_edge.clients.polymarket import PolymarketClient
from kalshi_btc_edge.clients.spot import SpotClient
from kalshi_btc_edge.config import AppConfig
from kalshi_btc_edge.execution.paper import PaperBroker
from kalshi_btc_edge.execution.risk import size_trade
from kalshi_btc_edge.models import Confidence, EdgeSignal
from kalshi_btc_edge.pricing.smile import load_smile
from kalshi_btc_edge.strategies.cross_venue import format_arb, scan_cross_venue
from kalshi_btc_edge.strategies.mispricing import format_signal, scan_mispricing

log = logging.getLogger(__name__)


class EdgeBot:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.kalshi = KalshiClient(cfg.markets.kalshi_base_url)
        self.spot = SpotClient()
        self.poly = PolymarketClient(cfg.cross_venue.polymarket_gamma_url)
        self.broker = PaperBroker()

    def live_enabled(self) -> bool:
        if self.cfg.execution.mode != "live":
            return False
        if self.cfg.execution.require_env_live_flag:
            return os.environ.get("ENABLE_LIVE_TRADING") == "1"
        return True

    def fetch_markets(self):
        markets = []
        for series in self.cfg.markets.series:
            try:
                batch = self.kalshi.get_markets(
                    series_ticker=series,
                    status=self.cfg.markets.status,
                    limit=self.cfg.markets.page_limit,
                )
                log.info("fetched %d %s markets", len(batch), series)
                markets.extend(batch)
            except Exception as exc:  # noqa: BLE001
                log.error("kalshi fetch %s failed: %s", series, exc)
        return markets

    def run_once(self) -> dict:
        markets = self.fetch_markets()
        smile = load_smile(self.cfg.pricing, self.cfg.root)
        try:
            btc_spot = self.spot.btc_usd(self.cfg.pricing.btc_spot_source)
        except Exception as exc:  # noqa: BLE001
            log.error("btc spot failed: %s", exc)
            raise
        try:
            ibit_spot = self.spot.ibit_usd(
                self.cfg.pricing.ibit_spot_source, fallback=smile.spot
            )
        except Exception:
            ibit_spot = smile.spot

        smile.spot = ibit_spot
        signals = scan_mispricing(markets, btc_spot, ibit_spot, smile, self.cfg)

        actionable = [
            s
            for s in signals
            if s.confidence != Confidence.PASS
        ]
        for s in actionable[:20]:
            log.info("%s", format_signal(s))

        fills = []
        max_fills = self.cfg.execution.max_paper_fills_per_scan
        for s in signals:
            if len(fills) >= max_fills:
                break
            intent = size_trade(
                s,
                self.cfg.risk,
                open_notional=self.broker.open_notional,
                min_confidence=self.cfg.execution.min_confidence,
            )
            if intent is None:
                continue
            if self.live_enabled():
                log.error(
                    "LIVE mode requested but live order routing is not wired; "
                    "refusing to send real orders. Paper fill only."
                )
            fills.append(self.broker.execute(intent))


        arbs = []
        if self.cfg.cross_venue.enabled:
            try:
                poly_mkts = self.poly.search_btc_15m()
                arbs = scan_cross_venue(markets, poly_mkts, self.cfg.cross_venue)
                for a in arbs[:10]:
                    log.info("%s", format_arb(a))
                    log.info("risk: %s", a.risk_note)
            except Exception as exc:  # noqa: BLE001
                log.warning("cross-venue scan failed: %s", exc)

        return {
            "markets": len(markets),
            "signals": signals,
            "actionable": actionable,
            "fills": fills,
            "arbs": arbs,
            "btc_spot": btc_spot,
            "ibit_spot": ibit_spot,
        }

    def run_loop(self, once: bool = False, max_iters: Optional[int] = None) -> None:
        n = 0
        while True:
            n += 1
            try:
                summary = self.run_once()
                log.info(
                    "scan done markets=%d actionable=%d fills=%d arbs=%d btc=%.2f",
                    summary["markets"],
                    len(summary["actionable"]),
                    len(summary["fills"]),
                    len(summary["arbs"]),
                    summary["btc_spot"],
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("scan failed: %s", exc)
            if once:
                break
            if max_iters is not None and n >= max_iters:
                break
            time.sleep(self.cfg.execution.poll_seconds)


def top_edges(signals: list[EdgeSignal], n: int = 10) -> list[EdgeSignal]:
    return sorted(signals, key=lambda s: abs(s.edge_pp), reverse=True)[:n]
