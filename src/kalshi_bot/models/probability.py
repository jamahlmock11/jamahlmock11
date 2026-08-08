"""Edge / confidence models and options-implied probability engine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from kalshi_bot.config import TierConfig
from kalshi_bot.models.black_scholes import annualize_horizon_years, binary_call_prob, scale_iv_to_horizon
from kalshi_bot.models.vol_smile import VolSmile, iv_for_btc_strike


class Confidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    PASS = "PASS"


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


@dataclass(frozen=True)
class ImpliedProb:
    options_prob: float
    iv_used: float
    spot: float
    strike: float
    t_years: float
    source: str


@dataclass(frozen=True)
class EdgeSignal:
    ticker: str
    series: str
    side: Side
    kalshi_prob: float  # executable price for the side we're buying
    options_prob: float
    edge_pp: float  # percentage points
    confidence: Confidence
    spread_cents: float
    book_usd: float
    strike: float
    spot: float
    iv: float
    t_years: float
    reason: str

    @property
    def edge_fraction(self) -> float:
        return self.edge_pp / 100.0


def classify_tier(
    edge_pp: float,
    spread_cents: float,
    book_usd: float,
    tiers: TierConfig,
) -> Confidence:
    """Display confidence tiers; execution independently enforces 20 points."""
    if edge_pp < tiers.low_pp:
        return Confidence.PASS
    if edge_pp >= tiers.high_pp and spread_cents <= tiers.tight_spread_cents and book_usd >= tiers.min_book_usd:
        return Confidence.HIGH
    if edge_pp >= tiers.medium_pp:
        return Confidence.MEDIUM
    return Confidence.LOW


def options_implied_prob_above(
    *,
    btc_spot: float,
    btc_strike: float,
    seconds_to_expiry: float,
    smile: VolSmile | None,
    ibit_spot: float,
    r: float,
    fallback_iv: float,
    min_iv: float,
    max_iv: float,
) -> ImpliedProb:
    """Black-Scholes N(d2) in BTC spot space using IBIT smile IV."""
    t = annualize_horizon_years(seconds_to_expiry)
    if smile is not None and smile.points:
        iv = iv_for_btc_strike(smile, btc_spot, btc_strike, ibit_spot, fallback_iv)
        iv = scale_iv_to_horizon(iv, smile.t_years, t)
        source = f"IBIT smile → BTC ({smile.underlying})"
    else:
        iv = fallback_iv
        source = "fallback IV"
    iv = max(min_iv, min(max_iv, iv))
    prob = binary_call_prob(btc_spot, btc_strike, t, r, iv)
    return ImpliedProb(
        options_prob=prob,
        iv_used=iv,
        spot=btc_spot,
        strike=btc_strike,
        t_years=t,
        source=source,
    )


def detect_mispricing(
    *,
    ticker: str,
    series: str,
    yes_bid: float,
    yes_ask: float,
    yes_ask_size: float,
    no_ask: float,
    no_ask_size: float,
    strike: float,
    spot: float,
    seconds_to_expiry: float,
    smile: VolSmile | None,
    ibit_spot: float,
    r: float,
    fallback_iv: float,
    min_iv: float,
    max_iv: float,
    tiers: TierConfig,
    notional: float = 1.0,
) -> EdgeSignal | None:
    """Compare Kalshi book vs options-implied probability; return best edge side."""
    implied = options_implied_prob_above(
        btc_spot=spot,
        btc_strike=strike,
        seconds_to_expiry=seconds_to_expiry,
        smile=smile,
        ibit_spot=ibit_spot,
        r=r,
        fallback_iv=fallback_iv,
        min_iv=min_iv,
        max_iv=max_iv,
    )
    p_opt = implied.options_prob
    spread_cents = (yes_ask - yes_bid) * 100.0

    # Buy YES if options >> Kalshi ask
    yes_edge_pp = (p_opt - yes_ask) * 100.0
    yes_book = yes_ask_size * yes_ask * notional

    # Buy NO if options << (1 - no_ask) i.e. options put prob >> no_ask
    # NO pays when S_T <= K; options P(NO) = 1 - p_opt
    p_no_opt = 1.0 - p_opt
    no_edge_pp = (p_no_opt - no_ask) * 100.0
    no_book = no_ask_size * no_ask * notional

    candidates: list[tuple[Side, float, float, float]] = [
        (Side.YES, yes_ask, yes_edge_pp, yes_book),
        (Side.NO, no_ask, no_edge_pp, no_book),
    ]
    side, kalshi_px, edge_pp, book = max(candidates, key=lambda x: x[2])
    conf = classify_tier(edge_pp, spread_cents, book, tiers)
    if conf is Confidence.PASS and edge_pp < tiers.low_pp:
        return EdgeSignal(
            ticker=ticker,
            series=series,
            side=side,
            kalshi_prob=kalshi_px,
            options_prob=p_opt if side is Side.YES else p_no_opt,
            edge_pp=edge_pp,
            confidence=Confidence.PASS,
            spread_cents=spread_cents,
            book_usd=book,
            strike=strike,
            spot=spot,
            iv=implied.iv_used,
            t_years=implied.t_years,
            reason=f"edge {edge_pp:.1f}pp below {tiers.low_pp}pp threshold",
        )

    reason = (
        f"options {p_opt*100:.1f}% vs Kalshi {side.value}@{kalshi_px*100:.1f}% "
        f"→ {edge_pp:.1f}pp ({implied.source}, IV={implied.iv_used*100:.1f}%)"
    )
    return EdgeSignal(
        ticker=ticker,
        series=series,
        side=side,
        kalshi_prob=kalshi_px,
        options_prob=p_opt if side is Side.YES else p_no_opt,
        edge_pp=edge_pp,
        confidence=conf,
        spread_cents=spread_cents,
        book_usd=book,
        strike=strike,
        spot=spot,
        iv=implied.iv_used,
        t_years=implied.t_years,
        reason=reason,
    )