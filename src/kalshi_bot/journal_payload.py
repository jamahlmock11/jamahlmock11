"""Shared decision-journal payload fragments for 15m and 1h bots."""

from __future__ import annotations

from typing import Any

from kalshi_bot.config import AppConfig
from kalshi_bot.domain import DecisionAction, FeatureSnapshot
from kalshi_bot.models.strike_gravity import assess_strike_gravity


def strategy_config_snapshot(config: AppConfig, *, horizon: str) -> dict[str, Any]:
    max_entry = (
        config.hour.max_entry_seconds_remaining
        if horizon == "1h"
        else config.strategy.max_entry_seconds_remaining
    )
    return {
        "min_edge": config.strategy.min_edge,
        "target_edge": config.strategy.target_edge,
        "final_min_edge": config.strategy.final_min_edge,
        "min_seconds_remaining": config.strategy.min_seconds_remaining,
        "max_entry_seconds_remaining": max_entry,
        "min_signal_agreement": config.strategy.min_signal_agreement,
        "min_data_completeness": config.strategy.min_data_completeness,
        "late_confidence_increment": config.strategy.late_confidence_increment,
        "min_entry_executable_cost": config.strategy.min_entry_executable_cost,
        "max_spread": config.strategy.max_spread,
        "min_confidence": config.strategy.min_confidence,
        "late_seconds": config.strategy.late_seconds,
        "final_seconds": config.strategy.final_seconds,
        "late_favorite_seconds": config.strategy.late_favorite_seconds,
        "late_favorite_poll_threshold": config.strategy.late_favorite_poll_threshold,
        "late_favorite_min_edge": config.strategy.late_favorite_min_edge,
        "minimum_dominant_poll": config.strategy.minimum_dominant_poll,
        "require_dominant_poll_side": config.strategy.require_dominant_poll_side,
        "min_trade_quality_score": config.strategy.min_trade_quality_score,
        "require_trade_quality": config.strategy.require_trade_quality,
        "kelly_fraction": config.risk.kelly_fraction,
        "kelly_bankroll_usd": config.risk.kelly_bankroll_usd,
        "kelly_max_fraction": config.risk.kelly_max_fraction,
        "min_hold_seconds": config.risk.min_hold_seconds,
        "follow_extreme_poll": config.longshot.follow_extreme_poll,
        "extreme_poll_threshold": config.longshot.extreme_poll_threshold,
        "crowd_follow_price_band_cents": config.longshot.crowd_follow_price_band_cents,
        "horizon": horizon,
    }


def strike_context_snapshot(features: FeatureSnapshot) -> dict[str, Any]:
    gravity = assess_strike_gravity(features)
    return {
        "seconds_remaining": features.seconds_remaining,
        "spot": features.current_price,
        "strike": (
            features.settlement_effective_strike
            if features.settlement_effective_strike is not None
            else features.strike
        ),
        "z_distance": features.z_distance_to_strike,
        "hold_up_probability": gravity.finish_probability_up,
        "late_momentum_pattern": features.late_momentum_pattern,
        "late_momentum_summary": features.late_momentum_summary,
        "late_momentum_drift": features.late_momentum_drift,
        "late_momentum_hammer": features.late_momentum_hammer,
        "late_momentum_fade": features.late_momentum_fade,
        "late_momentum_finish_bias": features.late_momentum_finish_bias,
    }


def decision_execution_snapshot(decision) -> dict[str, Any]:
    if decision is None:
        return {"required_edge": None, "kelly_contracts": None}
    kelly_contracts = None
    if decision.action in {DecisionAction.BUY_UP, DecisionAction.BUY_DOWN} and decision.quantity > 0:
        kelly_contracts = int(decision.quantity)
    return {
        "required_edge": decision.required_edge,
        "kelly_contracts": kelly_contracts,
    }
