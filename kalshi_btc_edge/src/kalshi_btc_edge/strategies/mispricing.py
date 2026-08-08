from __future__ import annotations

import logging
from typing import Optional

from kalshi_btc_edge.config import AppConfig
from kalshi_btc_edge.models import Confidence, EdgeSignal, KalshiMarket, Side, VolSmile
from kalshi_btc_edge.pricing.black_scholes import digital_call_prob, digital_call_prob_ge
from kalshi_btc_edge.pricing.edge import classify_confidence, compute_edge_pp, side_from_edge
from kalshi_btc_edge.pricing.smile import map_btc_strike_to_ibit, short_tenor_iv

log = logging.getLogger(__name__)


def options_implied_yes(
    market: KalshiMarket,
    btc_spot: float,
    ibit_spot: float,
    smile: VolSmile,
    r: float,
    q: float,
) -> tuple[float, float, str]:
    """Return (P_yes, iv_used, note) from IBIT smile mapped into BTC space."""
    t = market.years_to_close
    if market.floor_strike is None:
        return 0.5, smile.iv_at_moneyness(1.0), "missing floor_strike"

    strike = float(market.floor_strike)

    if market.is_updown:
        # YES if end BRTI >= open reference (floor_strike). ATM digital on open.
        # Price vs *current* spot: if spot already moved, strike ≠ spot.
        ibit_k = map_btc_strike_to_ibit(strike, btc_spot, ibit_spot)
        mny = ibit_k / ibit_spot
        iv = short_tenor_iv(smile, mny, t)
        p = digital_call_prob_ge(btc_spot, strike, t, iv, r=r, q=q)
        return p, iv, "KXBTC15M up/down digital vs open BRTI"

    # KXBTCD: YES if end BRTI > floor_strike
    ibit_k = map_btc_strike_to_ibit(strike, btc_spot, ibit_spot)
    mny = ibit_k / ibit_spot
    iv = short_tenor_iv(smile, mny, t)
    # Use BTC spot/strike with IV translated from IBIT smile moneyness
    p = digital_call_prob(btc_spot, strike, t, iv, r=r, q=q)
    return p, iv, "KXBTCD strike digital via IBIT smile"


def signal_for_market(
    market: KalshiMarket,
    btc_spot: float,
    ibit_spot: float,
    smile: VolSmile,
    cfg: AppConfig,
) -> Optional[EdgeSignal]:
    mid = market.book.mid
    if mid is None:
        return None
    if len(smile.points) < cfg.pricing.min_smile_points:
        log.warning("smile too thin (%d points); skipping", len(smile.points))
        return None

    p_yes, iv, note = options_implied_yes(
        market,
        btc_spot,
        ibit_spot,
        smile,
        r=cfg.pricing.risk_free_rate,
        q=cfg.pricing.dividend_yield,
    )
    edge_pp = compute_edge_pp(p_yes, mid)
    conf = classify_confidence(edge_pp, market.book.spread_cents, cfg.confidence)
    side = side_from_edge(edge_pp)
    return EdgeSignal(
        market_ticker=market.ticker,
        series=market.series_ticker,
        kalshi_mid=mid,
        options_prob_yes=p_yes,
        edge_pp=edge_pp,
        confidence=conf,
        side=side,
        strike_btc=market.floor_strike,
        btc_spot=btc_spot,
        iv_used=iv,
        spread_cents=market.book.spread_cents,
        reason=note,
    )


def scan_mispricing(
    markets: list[KalshiMarket],
    btc_spot: float,
    ibit_spot: float,
    smile: VolSmile,
    cfg: AppConfig,
) -> list[EdgeSignal]:
    signals: list[EdgeSignal] = []
    for m in markets:
        try:
            sig = signal_for_market(m, btc_spot, ibit_spot, smile, cfg)
        except Exception as exc:  # noqa: BLE001
            log.warning("mispricing skip %s: %s", m.ticker, exc)
            continue
        if sig is None:
            continue
        signals.append(sig)
    signals.sort(key=lambda s: abs(s.edge_pp), reverse=True)
    return signals


def format_signal(sig: EdgeSignal) -> str:
    return (
        f"{sig.confidence.value:6} {sig.side.value:3} {sig.market_ticker} "
        f"kalshi={sig.kalshi_mid:.1%} opt={sig.options_prob_yes:.1%} "
        f"edge={sig.edge_pp:+.1f}pp iv={sig.iv_used:.1%} "
        f"spread={sig.spread_cents if sig.spread_cents is not None else float('nan'):.1f}¢ "
        f"K={sig.strike_btc} spot={sig.btc_spot:.2f}"
    )
