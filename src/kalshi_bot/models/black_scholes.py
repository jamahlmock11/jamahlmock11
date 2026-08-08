"""Black-Scholes primitives for binary / digital probability pricing."""

from __future__ import annotations

import math

from scipy.stats import norm


def d1(spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    if spot <= 0 or strike <= 0 or t <= 0 or sigma <= 0:
        raise ValueError("spot, strike, t, sigma must be positive")
    return (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * t) / (sigma * math.sqrt(t))


def d2(spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    return d1(spot, strike, t, r, sigma) - sigma * math.sqrt(t)


def call_price(spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    _d1 = d1(spot, strike, t, r, sigma)
    _d2 = _d1 - sigma * math.sqrt(t)
    return spot * norm.cdf(_d1) - strike * math.exp(-r * t) * norm.cdf(_d2)


def put_price(spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    _d1 = d1(spot, strike, t, r, sigma)
    _d2 = _d1 - sigma * math.sqrt(t)
    return strike * math.exp(-r * t) * norm.cdf(-_d2) - spot * norm.cdf(-_d1)


def binary_call_prob(spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    """Risk-neutral P(S_T > K) = N(d2).

    This is the cash-or-nothing (digital) probability under GBM — the correct
    comparison object for Kalshi YES prices on threshold / up-or-down markets.
    """
    if t <= 0:
        return 1.0 if spot > strike else (0.5 if spot == strike else 0.0)
    if sigma <= 0:
        return 1.0 if spot > strike else 0.0
    return float(norm.cdf(d2(spot, strike, t, r, sigma)))


def binary_put_prob(spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    return 1.0 - binary_call_prob(spot, strike, t, r, sigma)


def annualize_horizon_years(seconds: float) -> float:
    return max(seconds, 1.0) / (365.25 * 24 * 3600)


def scale_iv_to_horizon(iv_at_t1: float, t1_years: float, t2_years: float) -> float:
    """IV transport for short Kalshi horizons (15m / 1h).

    IBIT listed expiries are days/weeks; Kalshi windows are much shorter.
    Ultra-short BTC realized vol is typically elevated vs listed IBIT IV
    (target regime ~50-80%), so we apply a stronger short-tenor premium and
    floor very short horizons toward 50% when the smile prints too quiet.
    """
    if t1_years <= 0 or t2_years <= 0:
        return iv_at_t1
    ratio = math.sqrt(t1_years / t2_years)
    bump = 1.0 + 0.15 * max(0.0, math.log(max(ratio, 1.0)))
    iv = iv_at_t1 * bump
    # Sub-2h windows: soft floor into BTC high-vol regime
    hours = t2_years * 365.25 * 24
    if hours <= 2.0:
        iv = max(iv, 0.50)
    return iv


def moneyness(spot: float, strike: float) -> float:
    return math.log(strike / spot)