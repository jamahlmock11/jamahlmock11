"""SentimentAgent — blends momentum and orderbook skew into a directional score."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.domain import FeatureSnapshot, MarketSnapshot
from kalshi_bot.market.orderbook import skew_top_n


@dataclass(frozen=True)
class SentimentVerdict:
    score: float
    label: str
    explanation: str


def _momentum_score(features: FeatureSnapshot) -> float:
    """Normalize recent price drift into [-1, 1]."""
    changes = features.changes or {}
    long_horizon = changes.get(3600) or changes.get(1800) or changes.get(900)
    if long_horizon is None:
        long_horizon = features.short_trend
    return max(-1.0, min(1.0, long_horizon * 500.0))


def evaluate_sentiment(
    features: FeatureSnapshot,
    market: MarketSnapshot,
    *,
    momentum_weight: float = 0.5,
    skew_weight: float = 0.5,
) -> SentimentVerdict:
    momentum = _momentum_score(features)
    skew = skew_top_n(market.orderbook, n=5)
    total = max(momentum_weight + skew_weight, 1e-9)
    score = (momentum_weight * momentum + skew_weight * skew) / total
    score = max(-1.0, min(1.0, score))

    if score > 0.15:
        label = "bullish"
    elif score < -0.15:
        label = "bearish"
    else:
        label = "neutral"

    parts: list[str] = []
    if abs(momentum) >= 0.15:
        parts.append(f"{'strong' if abs(momentum) > 0.35 else 'mild'} momentum")
    if abs(skew) >= 0.15:
        parts.append(f"{'bullish' if skew > 0 else 'bearish'} orderbook skew")
    if momentum * skew < -0.02 and parts:
        explanation = f"{' '.join(parts[:1])} offset by {parts[-1]}"
    elif parts:
        explanation = " and ".join(parts)
    else:
        explanation = "no strong directional signal"

    return SentimentVerdict(score=score, label=label, explanation=explanation)
