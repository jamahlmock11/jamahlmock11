"""Time-tiered minimum net edge: model probability minus executable Kalshi price."""

from __future__ import annotations

from kalshi_bot.config import DynamicEdgeBand, StrategyConfig


def required_edge_from_bands(
    seconds_remaining: float,
    bands: list[DynamicEdgeBand],
    fallback: float,
) -> float:
    """
    Map time remaining to a minimum net edge using half-open minute bands (min, max].

    Example bands for 15m: (10,15]→10¢, (7,10]→10¢, (5,7]→8¢, (3,5]→8¢, (1,3]→6¢.
    """
    minutes = max(seconds_remaining, 0.0) / 60.0
    for band in bands:
        if band.min_minutes < minutes <= band.max_minutes:
            return band.min_edge
    return fallback


def strategy_minimum_edge_floor(strategy: StrategyConfig) -> float:
    """Lowest configured edge floor (for risk hard-min when tiers are enabled)."""
    if strategy.dynamic_edge_enabled and strategy.dynamic_edge_bands:
        return min(band.min_edge for band in strategy.dynamic_edge_bands)
    return strategy.min_edge


def strategy_required_edge(
    seconds_remaining: float,
    strategy: StrategyConfig,
) -> float:
    """Primary mispricing gate: required net edge from time remaining."""
    if strategy.dynamic_edge_enabled and strategy.dynamic_edge_bands:
        return required_edge_from_bands(
            seconds_remaining,
            strategy.dynamic_edge_bands,
            strategy.min_edge,
        )
    return strategy.min_edge


def late_favorite_required_edge(
    seconds_remaining: float,
    *,
    late_favorite_min_edge: float,
    late_favorite_edge_bands: list[DynamicEdgeBand],
) -> float:
    """Late-favorite shortcut edge floor when poll + model confirmations pass."""
    if late_favorite_edge_bands:
        return required_edge_from_bands(
            seconds_remaining,
            late_favorite_edge_bands,
            late_favorite_min_edge,
        )
    return late_favorite_min_edge
