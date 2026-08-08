"""Black-Scholes digital (binary cash-or-nothing) probabilities.

Kalshi YES on a greater/greater_or_equal BTC strike settles like a digital call
on BRTI. Under BS, P*(S_T > K) in the stock-numeraire / risk-neutral measure
used for cash-or-nothing is N(d2).
"""

from __future__ import annotations

import math

from scipy.stats import norm


def _d1_d2(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    r: float,
    q: float,
) -> tuple[float, float]:
    if spot <= 0 or strike <= 0:
        raise ValueError("spot and strike must be positive")
    if t_years <= 0:
        # At expiry: digital is 1 iff ITM
        return (math.inf if spot > strike else -math.inf), (
            math.inf if spot > strike else -math.inf
        )
    if iv <= 0:
        forward = spot * math.exp((r - q) * t_years)
        d = math.inf if forward > strike else -math.inf
        return d, d
    vol_sqrt_t = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * iv * iv) * t_years) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    return d1, d2


def digital_call_prob(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    r: float = 0.0,
    q: float = 0.0,
) -> float:
    """Risk-neutral P(S_T > K) ≈ N(d2)."""
    if t_years <= 0:
        return 1.0 if spot > strike else 0.0
    _, d2 = _d1_d2(spot, strike, t_years, iv, r, q)
    return float(norm.cdf(d2))


def digital_put_prob(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    r: float = 0.0,
    q: float = 0.0,
) -> float:
    return 1.0 - digital_call_prob(spot, strike, t_years, iv, r, q)


def digital_call_prob_ge(
    spot: float,
    strike: float,
    t_years: float,
    iv: float,
    r: float = 0.0,
    q: float = 0.0,
) -> float:
    """P(S_T >= K). Continuous BS: same as P(S_T > K)."""
    if t_years <= 0:
        return 1.0 if spot >= strike else 0.0
    return digital_call_prob(spot, strike, t_years, iv, r, q)
