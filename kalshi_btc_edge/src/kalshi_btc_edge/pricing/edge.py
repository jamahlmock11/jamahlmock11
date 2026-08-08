from __future__ import annotations

from typing import Optional

from kalshi_btc_edge.config import ConfidenceConfig
from kalshi_btc_edge.models import Confidence, Side


def compute_edge_pp(options_prob_yes: float, kalshi_mid: float) -> float:
    """Edge in percentage points: (options_implied - kalshi_mid) * 100.

    Example: Kalshi 22% vs options 37.8% → +15.8pp → buy YES.
    Negative edge means buy NO (options say YES is overpriced on Kalshi).
    """
    return (options_prob_yes - kalshi_mid) * 100.0


def side_from_edge(edge_pp: float) -> Side:
    return Side.YES if edge_pp >= 0 else Side.NO


def classify_confidence(
    edge_pp: float,
    spread_cents: Optional[float],
    cfg: ConfidenceConfig,
) -> Confidence:
    """Confidence tiers for BTC 50–80% IV regime.

    HIGH  ≥15pp with tight book
    MEDIUM ≥10pp
    LOW   5–10pp
    PASS  below 5pp (or above max_credible_edge_pp — likely model error)
    """
    abs_pp = abs(edge_pp)
    if abs_pp < cfg.low_pp:
        return Confidence.PASS
    if abs_pp > cfg.max_credible_edge_pp:
        # Sample/uncalibrated smiles routinely print 30–40pp "edges" that are
        # not tradeable alpha. Force PASS until the surface is trusted.
        return Confidence.PASS
    if abs_pp >= cfg.high_pp:
        if spread_cents is not None and spread_cents <= cfg.high_max_spread_cents:
            return Confidence.HIGH
        # Wide book demotes HIGH → MEDIUM
        if abs_pp >= cfg.medium_pp:
            return Confidence.MEDIUM
        return Confidence.LOW
    if abs_pp >= cfg.medium_pp:
        if spread_cents is not None and spread_cents > cfg.medium_max_spread_cents:
            return Confidence.LOW
        return Confidence.MEDIUM
    return Confidence.LOW
