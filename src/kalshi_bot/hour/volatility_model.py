"""Volatility analysis for 1-hour BTC contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass

SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60


@dataclass(frozen=True)
class VolatilitySnapshot:
    realized_vol: float
    short_vol: float
    rolling_vol: float
    vol_expansion: float
    vol_contraction: float
    atr_like: float
    expected_move: float
    normalized_strike_distance: float


def analyze_volatility(
    *,
    current_price: float,
    strike: float,
    seconds_remaining: float,
    realized_vol: float,
    changes: dict[int, float],
    prices: list[float],
    timestamps_span: float,
) -> VolatilitySnapshot:
    short_returns = [changes[h] for h in (5, 15, 30, 60) if h in changes]
    short_vol = (
        math.sqrt(sum(r * r for r in short_returns) / len(short_returns))
        * math.sqrt(SECONDS_PER_YEAR / 60.0)
        if short_returns
        else realized_vol
    )

    rolling_vol = realized_vol
    if len(prices) > 2 and timestamps_span > 0:
        log_rets = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0 and prices[i] > 0:
                log_rets.append(math.log(prices[i] / prices[i - 1]))
        if log_rets:
            var = sum(r * r for r in log_rets) / len(log_rets)
            rolling_vol = math.sqrt(var * SECONDS_PER_YEAR / max(timestamps_span, 1.0))

    vol_expansion = max(0.0, short_vol / max(realized_vol, 0.01) - 1.0)
    vol_contraction = max(0.0, realized_vol / max(short_vol, 0.01) - 1.0)

    atr_like = 0.0
    if prices:
        ranges = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
        atr_like = sum(ranges) / len(ranges) if ranges else 0.0

    years = max(seconds_remaining, 1.0) / SECONDS_PER_YEAR
    expected_move = current_price * realized_vol * math.sqrt(years)
    strike_distance = current_price - strike
    normalized = strike_distance / expected_move if expected_move > 0 else 0.0

    return VolatilitySnapshot(
        realized_vol=realized_vol,
        short_vol=short_vol,
        rolling_vol=rolling_vol,
        vol_expansion=vol_expansion,
        vol_contraction=vol_contraction,
        atr_like=atr_like,
        expected_move=expected_move,
        normalized_strike_distance=normalized,
    )
