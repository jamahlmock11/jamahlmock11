"""Dynamic edge requirements and trade tier classification."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.config import HourEdgeConfig, HourStrategyConfig
from kalshi_bot.domain import EntryTiming, Regime, TradeTier
from kalshi_bot.hour.trend_engine import TrendSnapshot
from kalshi_bot.hour.volatility_model import VolatilitySnapshot


@dataclass(frozen=True)
class EdgeAssessment:
    required_edge: float
    trade_tier: TradeTier
    entry_timing: EntryTiming
    size_multiplier: float
    up_edge: float | None = None
    down_edge: float | None = None


def classify_entry_timing(
    seconds_remaining: float,
    trend: TrendSnapshot,
    *,
    late_window: float,
    contract_duration: float,
) -> EntryTiming:
    fraction_elapsed = 1.0 - seconds_remaining / contract_duration
    if seconds_remaining <= late_window:
        return EntryTiming.LATE
    if trend.trend_consistency >= 0.75 and abs(trend.medium_trend) > 0.0004:
        return EntryTiming.CONFIRMED
    if fraction_elapsed < 0.25:
        return EntryTiming.EARLY
    return EntryTiming.DEVELOPING


def required_edge(
    *,
    seconds_remaining: float,
    volatility: VolatilitySnapshot,
    spread: float,
    depth: float,
    confidence: float,
    agreement: float,
    regime: Regime,
    z_distance: float,
    trend: TrendSnapshot,
    model_stability: float,
    hour_cfg: HourStrategyConfig,
    edge_cfg: HourEdgeConfig,
    entry_timing: EntryTiming,
    is_proxy: bool = False,
) -> float:
    base = edge_cfg.minimum_edge

    if regime in {Regime.HIGH_VOLATILITY, Regime.UNCERTAIN, Regime.CHOPPY}:
        base = max(base, edge_cfg.preferred_edge)
    if spread > 0.08:
        base = max(base, edge_cfg.preferred_edge)
    if depth < 5.0:
        base = max(base, edge_cfg.preferred_edge + 0.05)
    if confidence < hour_cfg.min_confidence + 0.05:
        base = max(base, edge_cfg.preferred_edge)
    if agreement < hour_cfg.min_signal_agreement + 0.05:
        base = max(base, edge_cfg.preferred_edge)

    if agreement >= 0.80 and confidence >= 0.70 and spread <= 0.06:
        base = max(edge_cfg.minimum_edge, base - 0.02)

    late_frac = seconds_remaining / hour_cfg.contract_duration_seconds
    if late_frac <= hour_cfg.late_window_seconds / hour_cfg.contract_duration_seconds:
        base = max(base, edge_cfg.preferred_edge)
    if seconds_remaining <= hour_cfg.final_seconds:
        base = max(base, edge_cfg.strong_edge)

    if entry_timing == EntryTiming.LATE:
        base = max(base, edge_cfg.preferred_edge)
    if entry_timing == EntryTiming.EARLY:
        base = max(base, edge_cfg.minimum_edge + 0.02)

    if abs(z_distance) > 2.0 and volatility.vol_expansion > 0.2:
        base = max(base, edge_cfg.minimum_edge)

    if model_stability < 0.6:
        base = max(base, edge_cfg.preferred_edge)

    if is_proxy:
        base = max(base, edge_cfg.preferred_edge)

    return min(max(base, edge_cfg.minimum_edge), edge_cfg.strong_edge + 0.05)


def classify_trade_tier(
    edge: float,
    confidence: float,
    agreement: float,
    *,
    edge_cfg: HourEdgeConfig,
    hour_cfg: HourStrategyConfig,
    spread: float,
    depth: float,
) -> TradeTier:
    if edge_cfg.disable_tier_b and edge < edge_cfg.preferred_edge:
        return TradeTier.NONE

    liquidity_ok = spread <= hour_cfg.max_spread and depth >= hour_cfg.order_quantity
    if not liquidity_ok:
        return TradeTier.NONE

    if edge >= edge_cfg.strong_edge and confidence >= 0.70 and agreement >= 0.75:
        return TradeTier.A_PLUS
    if edge >= edge_cfg.preferred_edge and confidence >= hour_cfg.min_confidence and agreement >= hour_cfg.min_signal_agreement:
        return TradeTier.A
    if edge >= edge_cfg.minimum_edge and confidence >= hour_cfg.tier_b_min_confidence and agreement >= hour_cfg.tier_b_min_agreement:
        return TradeTier.B
    return TradeTier.NONE


def tier_size_multiplier(tier: TradeTier, edge_cfg: HourEdgeConfig) -> float:
    if tier is TradeTier.A_PLUS:
        return edge_cfg.tier_a_plus_size_mult
    if tier is TradeTier.A:
        return edge_cfg.tier_a_size_mult
    if tier is TradeTier.B:
        return edge_cfg.tier_b_size_mult
    return 0.0


def assess_edge(
    *,
    up_probability: float,
    down_probability: float,
    up_executable: float | None,
    down_executable: float | None,
    seconds_remaining: float,
    volatility: VolatilitySnapshot,
    yes_spread: float,
    no_spread: float,
    yes_depth: float,
    no_depth: float,
    confidence: float,
    agreement: float,
    regime: Regime,
    z_distance: float,
    trend: TrendSnapshot,
    model_stability: float,
    hour_cfg: HourStrategyConfig,
    edge_cfg: HourEdgeConfig,
    is_proxy: bool = False,
) -> EdgeAssessment:
    up_edge = up_probability - up_executable if up_executable is not None else None
    down_edge = down_probability - down_executable if down_executable is not None else None

    entry_timing = classify_entry_timing(
        seconds_remaining,
        trend,
        late_window=hour_cfg.late_window_seconds,
        contract_duration=hour_cfg.contract_duration_seconds,
    )

    spread = max(yes_spread, no_spread)
    depth = min(yes_depth, no_depth)

    req = required_edge(
        seconds_remaining=seconds_remaining,
        volatility=volatility,
        spread=spread,
        depth=depth,
        confidence=confidence,
        agreement=agreement,
        regime=regime,
        z_distance=z_distance,
        trend=trend,
        model_stability=model_stability,
        hour_cfg=hour_cfg,
        edge_cfg=edge_cfg,
        entry_timing=entry_timing,
        is_proxy=is_proxy,
    )

    best_edge = max(
        up_edge if up_edge is not None else -1.0,
        down_edge if down_edge is not None else -1.0,
    )
    tier = classify_trade_tier(
        best_edge,
        confidence,
        agreement,
        edge_cfg=edge_cfg,
        hour_cfg=hour_cfg,
        spread=spread,
        depth=depth,
    )
    if best_edge < req:
        tier = TradeTier.NONE

    return EdgeAssessment(
        required_edge=req,
        trade_tier=tier,
        entry_timing=entry_timing,
        size_multiplier=tier_size_multiplier(tier, edge_cfg),
        up_edge=up_edge,
        down_edge=down_edge,
    )
