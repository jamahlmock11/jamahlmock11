"""User-facing trading regimes with per-regime signal weights."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from kalshi_bot.domain import FeatureSnapshot, Regime


class TradingRegimeKind(str, Enum):
    TRENDING = "TRENDING"
    CHOPPY = "CHOPPY"
    BREAKING_OUT = "BREAKING_OUT"
    MEAN_REVERTING = "MEAN_REVERTING"
    NEWS_DRIVEN = "NEWS_DRIVEN"


@dataclass(frozen=True)
class TradingRegime:
    kind: TradingRegimeKind
    label: str
    signal_weights: dict[str, float]


# Per-regime signal weights (EMA, RSI, VWAP, Bollinger, Orderbook, News)
REGIME_WEIGHTS: dict[TradingRegimeKind, dict[str, float]] = {
    TradingRegimeKind.TRENDING: {
        "ema": 0.40,
        "rsi": 0.05,
        "vwap": 0.10,
        "bollinger": 0.05,
        "orderbook": 0.15,
        "news": 0.15,
    },
    TradingRegimeKind.CHOPPY: {
        "ema": 0.10,
        "rsi": 0.25,
        "vwap": 0.15,
        "bollinger": 0.20,
        "orderbook": 0.15,
        "news": 0.15,
    },
    TradingRegimeKind.BREAKING_OUT: {
        "ema": 0.30,
        "rsi": 0.10,
        "vwap": 0.10,
        "bollinger": 0.25,
        "orderbook": 0.20,
        "news": 0.15,
    },
    TradingRegimeKind.MEAN_REVERTING: {
        "ema": 0.15,
        "rsi": 0.35,
        "vwap": 0.20,
        "bollinger": 0.30,
        "orderbook": 0.10,
        "news": 0.10,
    },
    TradingRegimeKind.NEWS_DRIVEN: {
        "ema": 0.10,
        "rsi": 0.05,
        "vwap": 0.05,
        "bollinger": 0.10,
        "orderbook": 0.40,
        "news": 0.40,
    },
}


def classify_trading_regime(features: FeatureSnapshot, regime: Regime) -> TradingRegime:
    """Map internal regime + features to user-facing trading regime."""
    vol_high = features.realized_vol >= 0.75
    dispersion_high = features.cross_venue_dispersion >= 0.002
    news_driven = vol_high and dispersion_high

    if news_driven:
        kind = TradingRegimeKind.NEWS_DRIVEN
        label = "News"
    elif regime in {Regime.BREAKOUT, Regime.BREAKDOWN}:
        kind = TradingRegimeKind.BREAKING_OUT
        label = "Breakout"
    elif regime in {Regime.TREND_UP, Regime.TREND_DOWN}:
        kind = TradingRegimeKind.TRENDING
        label = "Trend"
    elif regime in {Regime.RANGE, Regime.LOW_VOLATILITY}:
        kind = TradingRegimeKind.MEAN_REVERTING
        label = "Range"
    elif regime is Regime.CHAOTIC_UNSTABLE:
        kind = TradingRegimeKind.CHOPPY
        label = "Choppy"
    elif regime in {Regime.REVERSAL_UP, Regime.REVERSAL_DOWN}:
        kind = TradingRegimeKind.MEAN_REVERTING
        label = "Reversal"
    elif regime is Regime.HIGH_VOLATILITY:
        kind = TradingRegimeKind.NEWS_DRIVEN
        label = "High Vol"
    else:
        kind = TradingRegimeKind.CHOPPY
        label = "Choppy"

    return TradingRegime(
        kind=kind,
        label=label,
        signal_weights=dict(REGIME_WEIGHTS[kind]),
    )


def blend_signal_probability(
    signal_probs: dict[str, float],
    weights: dict[str, float],
) -> float:
    """Weighted blend of signal probabilities."""
    total_weight = sum(weights.get(name, 0.0) for name in signal_probs)
    if total_weight <= 0:
        return 0.5
    weighted = sum(signal_probs[name] * weights.get(name, 0.0) for name in signal_probs)
    return weighted / total_weight
