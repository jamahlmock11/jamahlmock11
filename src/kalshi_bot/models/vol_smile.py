"""IBIT volatility smile construction and strike → IV interpolation."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.interpolate import PchipInterpolator

from kalshi_bot.models.black_scholes import moneyness, scale_iv_to_horizon


@dataclass
class SmilePoint:
    strike: float
    iv: float
    option_type: str  # "call" | "put"
    mid: float | None = None
    oi: float = 0.0


@dataclass
class VolSmile:
    """OTM IV smile for a single expiry, in IBIT space."""

    underlying: str
    spot: float
    expiry_ts: float
    t_years: float
    points: list[SmilePoint] = field(default_factory=list)
    _interp: PchipInterpolator | None = field(default=None, repr=False)

    def build(self) -> "VolSmile":
        if len(self.points) < 2:
            self._interp = None
            return self
        xs = np.array([moneyness(self.spot, p.strike) for p in self.points], dtype=float)
        ys = np.array([p.iv for p in self.points], dtype=float)
        order = np.argsort(xs)
        xs, ys = xs[order], ys[order]
        uniq_x: list[float] = []
        uniq_y: list[float] = []
        for x, y in zip(xs, ys):
            if not uniq_x or abs(x - uniq_x[-1]) > 1e-9:
                uniq_x.append(float(x))
                uniq_y.append(float(y))
        if len(uniq_x) < 2:
            self._interp = None
            return self
        self._interp = PchipInterpolator(uniq_x, uniq_y, extrapolate=True)
        return self

    def iv_at_strike(self, strike: float, fallback: float = 0.60) -> float:
        if self._interp is None:
            if not self.points:
                return fallback
            return min(self.points, key=lambda p: abs(p.strike - strike)).iv
        m = moneyness(self.spot, strike)
        iv = float(self._interp(m))
        return max(0.05, min(iv, 3.0))

    def atm_iv(self, fallback: float = 0.60) -> float:
        return self.iv_at_strike(self.spot, fallback=fallback)


def select_otm_points(spot: float, calls: list[SmilePoint], puts: list[SmilePoint]) -> list[SmilePoint]:
    """Desk convention: OTM puts below spot, OTM calls at/above spot."""
    otm: list[SmilePoint] = []
    for p in puts:
        if p.strike < spot and p.iv > 0:
            otm.append(p)
    for c in calls:
        if c.strike >= spot and c.iv > 0:
            otm.append(c)
    return otm


def blend_smiles(near: VolSmile, far: VolSmile | None, target_t: float, fallback_iv: float) -> float:
    """Return ATM IV transported toward target_t using nearest smile(s)."""
    if far is None or far.t_years <= near.t_years:
        return scale_iv_to_horizon(near.atm_iv(fallback_iv), near.t_years, target_t)
    if target_t <= near.t_years:
        return scale_iv_to_horizon(near.atm_iv(fallback_iv), near.t_years, target_t)
    if target_t >= far.t_years:
        return scale_iv_to_horizon(far.atm_iv(fallback_iv), far.t_years, target_t)
    w = (target_t - near.t_years) / (far.t_years - near.t_years)
    v_near = near.atm_iv(fallback_iv) ** 2 * near.t_years
    v_far = far.atm_iv(fallback_iv) ** 2 * far.t_years
    total_var = (1 - w) * v_near + w * v_far
    return math.sqrt(max(total_var / target_t, 1e-8))


def iv_for_btc_strike(
    smile: VolSmile,
    btc_spot: float,
    btc_strike: float,
    ibit_spot: float,
    fallback_iv: float,
) -> float:
    """Map a BTC strike into IBIT strike space via live price ratio, then read smile IV.

    IBIT tracks BTC NAV; the conversion factor is dynamic:
        K_ibit = K_btc * (IBIT_spot / BTC_spot)
    """
    if btc_spot <= 0 or ibit_spot <= 0:
        return fallback_iv
    ratio = ibit_spot / btc_spot
    ibit_strike = btc_strike * ratio
    return smile.iv_at_strike(ibit_strike, fallback=fallback_iv)