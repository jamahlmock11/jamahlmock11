"""Multi-timeframe trend classification for 1-hour contracts."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.domain import TrendClassification


HOUR_HORIZONS = (5, 15, 30, 60, 180, 300, 600, 900, 1800, 3600)


@dataclass(frozen=True)
class TrendSnapshot:
    short_trend: float
    medium_trend: float
    long_trend: float
    trend_strength: float
    trend_consistency: float
    momentum: float
    acceleration: float
    rate_of_change: float
    classification: TrendClassification
    horizon_signs: dict[int, int]


def _sign(value: float, threshold: float) -> int:
    if value > threshold:
        return 1
    if value < -threshold:
        return -1
    return 0


def classify_trend(
    changes: dict[int, float],
    *,
    short_horizons: tuple[int, ...] = (5, 15, 30),
    medium_horizons: tuple[int, ...] = (60, 180, 300),
    long_horizons: tuple[int, ...] = (600, 900, 1800, 3600),
    flat_threshold: float = 0.00005,
) -> TrendSnapshot:
    short_vals = [changes[h] for h in short_horizons if h in changes]
    medium_vals = [changes[h] for h in medium_horizons if h in changes]
    long_vals = [changes[h] for h in long_horizons if h in changes]

    short_trend = sum(short_vals) / len(short_vals) if short_vals else 0.0
    medium_trend = sum(medium_vals) / len(medium_vals) if medium_vals else 0.0
    long_trend = sum(long_vals) / len(long_vals) if long_vals else 0.0

    horizon_signs = {
        h: _sign(changes[h], flat_threshold) for h in HOUR_HORIZONS if h in changes
    }
    signs = list(horizon_signs.values())
    if signs:
        trend_consistency = abs(sum(signs)) / len(signs)
    else:
        trend_consistency = 0.0

    trend_strength = abs(short_trend) + 0.5 * abs(medium_trend) + 0.25 * abs(long_trend)
    momentum = short_trend
    rate_of_change = short_trend - medium_trend
    fast_v = changes.get(5, changes.get(15, 0.0))
    slow_v = changes.get(30, changes.get(60, 0.0))
    acceleration = fast_v - slow_v

    composite = short_trend + 0.6 * medium_trend + 0.3 * long_trend
    strength = trend_strength + trend_consistency * 0.0005

    if composite >= 0.0015 and strength >= 0.002:
        classification = TrendClassification.STRONG_UP
    elif composite >= 0.0006:
        classification = TrendClassification.UP
    elif composite >= flat_threshold:
        classification = TrendClassification.WEAK_UP
    elif composite <= -0.0015 and strength >= 0.002:
        classification = TrendClassification.STRONG_DOWN
    elif composite <= -0.0006:
        classification = TrendClassification.DOWN
    elif composite <= -flat_threshold:
        classification = TrendClassification.WEAK_DOWN
    else:
        classification = TrendClassification.NEUTRAL

    return TrendSnapshot(
        short_trend=short_trend,
        medium_trend=medium_trend,
        long_trend=long_trend,
        trend_strength=trend_strength,
        trend_consistency=trend_consistency,
        momentum=momentum,
        acceleration=acceleration,
        rate_of_change=rate_of_change,
        classification=classification,
        horizon_signs=horizon_signs,
    )
