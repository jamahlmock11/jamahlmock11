from __future__ import annotations

import math
from typing import Optional

from kalshi_btc_edge.config import RiskConfig
from kalshi_btc_edge.models import Confidence, EdgeSignal, Side, TradeIntent


_CONF_RANK = {
    Confidence.PASS: 0,
    Confidence.LOW: 1,
    Confidence.MEDIUM: 2,
    Confidence.HIGH: 3,
}


def meets_min_confidence(sig: EdgeSignal, minimum: Confidence) -> bool:
    return _CONF_RANK[sig.confidence] >= _CONF_RANK[minimum] and sig.confidence != Confidence.PASS


def kelly_contracts(
    edge_pp: float,
    entry_price: float,
    bankroll: float,
    kelly_fraction: float,
) -> int:
    """Binary Kelly with fractional dampening.

    For price p and true p* = p + edge, f* = (p* - p) / (1 - p) when buying YES
    at p (simplified).
    """
    if entry_price <= 0 or entry_price >= 1:
        return 0
    edge = abs(edge_pp) / 100.0
    # Approximate edge as (p* - p); payout 1-p on win, lose p
    f_star = edge / max(1e-9, (1.0 - entry_price))
    f = max(0.0, min(1.0, f_star * kelly_fraction))
    notional = bankroll * f
    contracts = int(math.floor(notional / entry_price))
    return max(0, contracts)


def size_trade(
    sig: EdgeSignal,
    risk: RiskConfig,
    open_notional: float = 0.0,
    min_confidence: Confidence = Confidence.HIGH,
) -> Optional[TradeIntent]:
    if not meets_min_confidence(sig, min_confidence):
        return None
    if sig.spread_cents is not None and sig.spread_cents > risk.max_spread_cents:
        return None

    # Enter at ask for YES, at (1 - bid) ≈ no ask for NO
    if sig.side == Side.YES:
        # Use mid + half spread approximation when only mid known
        entry = min(0.99, max(0.01, sig.kalshi_mid + (sig.spread_cents or 0) / 200.0))
    else:
        entry = min(0.99, max(0.01, 1.0 - sig.kalshi_mid + (sig.spread_cents or 0) / 200.0))

    contracts = kelly_contracts(
        sig.edge_pp, entry, risk.bankroll_usd, risk.kelly_fraction
    )
    contracts = min(contracts, risk.max_contracts_per_market)
    notional = contracts * entry
    if notional > risk.max_notional_per_trade_usd:
        contracts = int(math.floor(risk.max_notional_per_trade_usd / entry))
        notional = contracts * entry
    if open_notional + notional > risk.max_open_notional_usd:
        remain = max(0.0, risk.max_open_notional_usd - open_notional)
        contracts = int(math.floor(remain / entry))
    if contracts <= 0:
        return None

    return TradeIntent(
        market_ticker=sig.market_ticker,
        side=sig.side,
        contracts=contracts,
        limit_price=round(entry, 4),
        confidence=sig.confidence,
        edge_pp=sig.edge_pp,
        strategy="ibit_smile_mispricing",
        paper=True,
    )
