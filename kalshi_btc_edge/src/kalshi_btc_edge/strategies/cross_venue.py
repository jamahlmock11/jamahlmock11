from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from kalshi_btc_edge.clients.polymarket import PolyMarket
from kalshi_btc_edge.config import CrossVenueConfig
from kalshi_btc_edge.models import CrossVenueArb, KalshiMarket

log = logging.getLogger(__name__)

RISK_NOTE = (
    "Combined ask < $1 is NOT risk-free unless settlement windows, oracles, "
    "and contract definitions match exactly. Kalshi uses CF Benchmarks BRTI; "
    "Polymarket may use a different index/window. Prefer explicit contract_map."
)


def _close_ts(m: KalshiMarket) -> datetime:
    ct = m.close_time
    if ct.tzinfo is None:
        return ct.replace(tzinfo=timezone.utc)
    return ct


def pair_markets(
    kalshi: list[KalshiMarket],
    poly: list[PolyMarket],
    cfg: CrossVenueConfig,
) -> list[tuple[KalshiMarket, PolyMarket, float]]:
    pairs: list[tuple[KalshiMarket, PolyMarket, float]] = []
    updown = [m for m in kalshi if m.is_updown]

    # Explicit map first
    poly_by_id = {p.id: p for p in poly}
    poly_by_slug = {p.slug: p for p in poly if p.slug}
    for ticker, ref in cfg.contract_map.items():
        km = next((m for m in updown if m.ticker == ticker), None)
        pm = poly_by_id.get(ref) or poly_by_slug.get(ref)
        if km and pm:
            delta = 0.0
            if pm.end_time:
                delta = abs((_close_ts(km) - pm.end_time).total_seconds())
            pairs.append((km, pm, delta))

    # Nearest-end-time heuristic
    for km in updown:
        if any(km is p[0] for p in pairs):
            continue
        best: Optional[tuple[PolyMarket, float]] = None
        for pm in poly:
            if pm.end_time is None:
                continue
            delta = abs((_close_ts(km) - pm.end_time).total_seconds())
            if delta > cfg.max_end_time_delta_seconds:
                continue
            if best is None or delta < best[1]:
                best = (pm, delta)
        if best:
            pairs.append((km, best[0], best[1]))
    return pairs


def scan_cross_venue(
    kalshi: list[KalshiMarket],
    poly: list[PolyMarket],
    cfg: CrossVenueConfig,
) -> list[CrossVenueArb]:
    """Flag when Kalshi UP + Polymarket DOWN (or reverse) sum below threshold."""
    if not cfg.enabled:
        return []
    arbs: list[CrossVenueArb] = []
    for km, pm, delta in pair_markets(kalshi, poly, cfg):
        # Path A: buy Kalshi YES (UP) + Poly NO (DOWN)
        k_yes = km.book.yes_ask if km.book.yes_ask > 0 else 1.0
        sum_a = k_yes + pm.no_ask
        if sum_a < cfg.arb_sum_threshold:
            arbs.append(
                CrossVenueArb(
                    kalshi_ticker=km.ticker,
                    polymarket_id=pm.id,
                    kalshi_side="YES",
                    poly_side="NO",
                    kalshi_ask=k_yes,
                    poly_ask=pm.no_ask,
                    combined_ask=sum_a,
                    edge_usd=cfg.arb_sum_threshold - sum_a,
                    end_time_delta_seconds=delta,
                    risk_note=RISK_NOTE,
                )
            )
        # Path B: buy Kalshi NO + Poly YES
        k_no = km.book.no_ask
        sum_b = k_no + pm.yes_ask
        if sum_b < cfg.arb_sum_threshold:
            arbs.append(
                CrossVenueArb(
                    kalshi_ticker=km.ticker,
                    polymarket_id=pm.id,
                    kalshi_side="NO",
                    poly_side="YES",
                    kalshi_ask=k_no,
                    poly_ask=pm.yes_ask,
                    combined_ask=sum_b,
                    edge_usd=cfg.arb_sum_threshold - sum_b,
                    end_time_delta_seconds=delta,
                    risk_note=RISK_NOTE,
                )
            )
    arbs.sort(key=lambda a: a.edge_usd, reverse=True)
    return arbs


def format_arb(arb: CrossVenueArb) -> str:
    return (
        f"ARB {arb.kalshi_ticker} {arb.kalshi_side}+Poly:{arb.poly_side} "
        f"sum=${arb.combined_ask:.3f} edge=${arb.edge_usd:.3f} "
        f"Δt={arb.end_time_delta_seconds:.0f}s poly={arb.polymarket_id}"
    )
