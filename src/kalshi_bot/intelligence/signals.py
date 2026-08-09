"""EMA, RSI, VWAP, Bollinger, and orderbook signals for learning and explainability."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from kalshi_bot.domain import FeatureSnapshot, OrderBookSnapshot
from kalshi_bot.market.orderbook import imbalance


@dataclass(frozen=True)
class TechnicalSignals:
    """Directional signals mapped to user-facing names."""

    ema: float  # probability UP from EMA trend
    rsi: float  # probability UP from RSI
    vwap: float  # probability UP from VWAP distance
    bollinger: float  # probability UP from Bollinger position
    orderbook: float  # probability UP from orderbook imbalance
    news: float  # probability UP from cross-venue / volatility shock
    ema_bearish: bool
    rsi_value: float
    vwap_distance_pct: float
    bollinger_position: float

    def as_probabilities(self) -> dict[str, float]:
        return {
            "ema": self.ema,
            "rsi": self.rsi,
            "vwap": self.vwap,
            "bollinger": self.bollinger,
            "orderbook": self.orderbook,
            "news": self.news,
        }

    def direction_labels(self) -> dict[str, str]:
        labels: dict[str, str] = {}
        for name, prob in self.as_probabilities().items():
            if prob >= 0.55:
                labels[name] = f"{prob:.0%} Buy"
            elif prob <= 0.45:
                labels[name] = f"{(1 - prob):.0%} Sell"
            else:
                labels[name] = "Neutral"
        return labels


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _rsi_from_changes(changes: dict[int, float], period: int = 14) -> float:
    """Approximate RSI from available horizon returns."""
    values = [changes[h] for h in sorted(changes) if h <= period * 5]
    if len(values) < 2:
        return 50.0
    gains = [v for v in values if v > 0]
    losses = [-v for v in values if v < 0]
    avg_gain = statistics.fmean(gains) if gains else 0.0
    avg_loss = statistics.fmean(losses) if losses else 0.0
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_technical_signals(
    features: FeatureSnapshot,
    orderbook: OrderBookSnapshot | None = None,
) -> TechnicalSignals:
    """Derive named signals from causal feature snapshots."""
    # EMA proxy: short vs medium trend alignment
    trend_strength = math.tanh(
        (features.short_trend + features.medium_trend) / max(features.expected_remaining_move / features.current_price, 1e-8)
    )
    ema_prob = _clip(0.5 + 0.25 * trend_strength)
    ema_bearish = features.short_trend < 0 and features.medium_trend <= 0

    rsi_value = _rsi_from_changes(dict(features.changes))
    rsi_prob = _clip(rsi_value / 100.0)

    # VWAP proxy: mean reversion score inverted (price vs rolling mean)
    vwap_distance_pct = -features.mean_reversion_score * 0.01
    vwap_prob = _clip(0.5 - 0.20 * features.mean_reversion_score)

    # Bollinger: z-distance to strike as band position proxy
    bollinger_position = features.z_distance_to_strike
    bollinger_prob = _clip(0.5 + 0.15 * math.tanh(bollinger_position))

    book_imbalance = imbalance(orderbook) if orderbook is not None else features.orderbook_imbalance
    orderbook_prob = _clip(0.5 + 0.22 * book_imbalance)

    # News / macro shock: high vol + venue disagreement
    vol_shock = min(features.realized_vol / 1.0, 1.0)
    venue_stress = min(features.cross_venue_dispersion / 0.003, 1.0)
    news_direction = -1.0 if features.short_trend < 0 else 1.0
    news_prob = _clip(
        0.5 + news_direction * 0.15 * vol_shock * (1.0 - features.cross_venue_agreement)
        + news_direction * 0.10 * venue_stress
    )

    return TechnicalSignals(
        ema=ema_prob,
        rsi=rsi_prob,
        vwap=vwap_prob,
        bollinger=bollinger_prob,
        orderbook=orderbook_prob,
        news=news_prob,
        ema_bearish=ema_bearish,
        rsi_value=rsi_value,
        vwap_distance_pct=vwap_distance_pct,
        bollinger_position=bollinger_position,
    )
