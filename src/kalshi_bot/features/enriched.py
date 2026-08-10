"""Unified enriched feature bundle for the 15-minute forecasting pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from kalshi_bot.domain import FeatureSnapshot, MarketSnapshot, Regime
from kalshi_bot.features.microstructure import MicrostructureSnapshot, MicrostructureTracker
from kalshi_bot.features.price_action import PriceActionSnapshot, compute_price_action
from kalshi_bot.features.temporal import TemporalSnapshot, TemporalWinRateStore, compute_temporal


@dataclass(frozen=True)
class EnrichedFeatures:
    """Extended feature set layered on top of causal BRTI features."""

    microstructure: MicrostructureSnapshot
    price_action: PriceActionSnapshot
    temporal: TemporalSnapshot

    def as_dict(self) -> dict[str, object]:
        return {
            "microstructure": {
                "bid_ask_imbalance": self.microstructure.bid_ask_imbalance,
                "depth_top10_total": self.microstructure.depth_top10_total,
                "whale_detected": self.microstructure.whale_detected,
                "cancellation_rate": self.microstructure.cancellation_rate,
                "new_order_pressure": self.microstructure.new_order_pressure,
                "spread_trend": self.microstructure.spread_trend,
                "trade_velocity": self.microstructure.trade_velocity,
                "liquidity_score": self.microstructure.liquidity_score,
            },
            "price_action": {
                "vwap_distance_pct": self.price_action.vwap_distance_pct,
                "momentum_15s": self.price_action.momentum_15s,
                "momentum_30s": self.price_action.momentum_30s,
                "momentum_60s": self.price_action.momentum_60s,
                "volatility_expansion": self.price_action.volatility_expansion,
                "breakout_detected": self.price_action.breakout_detected,
                "fake_breakout": self.price_action.fake_breakout,
            },
            "temporal": {
                "minutes_until_expiration": self.temporal.minutes_until_expiration,
                "day_of_week": self.temporal.day_of_week,
                "hour_of_day": self.temporal.hour_of_day,
                "market_session": self.temporal.market_session,
                "historical_win_rate": self.temporal.historical_win_rate,
                "historical_sample_count": self.temporal.historical_sample_count,
                "minute_bucket": self.temporal.minute_bucket,
            },
        }


class EnrichedFeatureEngine:
    """Compute microstructure, price-action, and temporal features each cycle."""

    def __init__(
        self,
        *,
        microstructure: MicrostructureTracker | None = None,
        win_rate_store: TemporalWinRateStore | None = None,
    ) -> None:
        self.microstructure = microstructure or MicrostructureTracker()
        self.win_rate_store = win_rate_store or TemporalWinRateStore()

    def compute(
        self,
        features: FeatureSnapshot,
        market: MarketSnapshot,
        regime: Regime,
        *,
        now: datetime | None = None,
    ) -> EnrichedFeatures:
        micro = self.microstructure.compute(market.orderbook, features)
        price = compute_price_action(features, regime)
        temporal = compute_temporal(
            market,
            now,
            win_rate_store=self.win_rate_store,
        )
        return EnrichedFeatures(
            microstructure=micro,
            price_action=price,
            temporal=temporal,
        )
