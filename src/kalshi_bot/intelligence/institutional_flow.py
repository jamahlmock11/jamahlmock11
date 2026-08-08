"""Institutional flow detection: whales, liquidity shocks, funding stress."""

from __future__ import annotations

import math
from dataclasses import dataclass

from kalshi_bot.domain import FeatureSnapshot, OrderBookSnapshot, SupportingAggregate


@dataclass(frozen=True)
class FlowAssessment:
    """Institutional flow signals that often precede indicator moves."""

    whale_detected: bool
    liquidity_removal: bool
    exchange_stress: bool
    funding_spike: bool
    liquidation_cluster: bool
    flow_direction: float  # -1 bearish to +1 bullish
    confidence_boost: float  # 0–0.15 when strong flow aligns
    reasons: tuple[str, ...]


class InstitutionalFlowDetector:
    """Detect flow events from orderbook and supporting feed anomalies."""

    def __init__(
        self,
        *,
        whale_notional_threshold: float = 5000.0,
        liquidity_drop_pct: float = 0.40,
        dispersion_stress: float = 0.0025,
        vol_spike_threshold: float = 0.90,
    ) -> None:
        self.whale_notional_threshold = whale_notional_threshold
        self.liquidity_drop_pct = liquidity_drop_pct
        self.dispersion_stress = dispersion_stress
        self.vol_spike_threshold = vol_spike_threshold
        self._prev_total_depth: float | None = None

    def assess(
        self,
        features: FeatureSnapshot,
        orderbook: OrderBookSnapshot,
        supporting: SupportingAggregate | None = None,
    ) -> FlowAssessment:
        reasons: list[str] = []
        flow_signals: list[float] = []

        yes_notional = sum(level.price * level.size for level in orderbook.yes_bids)
        no_notional = sum(level.price * level.size for level in orderbook.no_bids)
        total_notional = yes_notional + no_notional
        largest_level = max(
            max((level.size for level in orderbook.yes_bids), default=0.0),
            max((level.size for level in orderbook.no_bids), default=0.0),
        )
        whale_detected = largest_level * features.current_price > self.whale_notional_threshold
        if whale_detected:
            reasons.append("whale order detected in book")
            whale_side = 1.0 if yes_notional > no_notional else -1.0
            flow_signals.append(whale_side * 0.3)

        liquidity_removal = False
        if self._prev_total_depth is not None and self._prev_total_depth > 0:
            depth_now = sum(level.size for level in orderbook.yes_bids) + sum(
                level.size for level in orderbook.no_bids
            )
            drop = (self._prev_total_depth - depth_now) / self._prev_total_depth
            if drop >= self.liquidity_drop_pct:
                liquidity_removal = True
                reasons.append(f"sudden liquidity removal ({drop:.0%} depth drop)")
                flow_signals.append(-0.4)
        self._prev_total_depth = sum(level.size for level in orderbook.yes_bids) + sum(
            level.size for level in orderbook.no_bids
        )

        exchange_stress = features.cross_venue_dispersion >= self.dispersion_stress
        if exchange_stress:
            reasons.append("exchange venue dislocation stress")
            stress_dir = -1.0 if features.short_trend < 0 else 1.0
            flow_signals.append(stress_dir * 0.2)

        funding_spike = features.realized_vol >= self.vol_spike_threshold
        if funding_spike:
            reasons.append("volatility / funding spike regime")
            flow_signals.append(-0.15 if features.short_trend < 0 else 0.15)

        liquidation_cluster = (
            features.realized_vol >= 0.75
            and abs(features.acceleration) > 1e-6
            and features.trajectory.value.startswith("ACCELERATING")
        )
        if liquidation_cluster:
            reasons.append("liquidation cluster pattern (vol + acceleration)")
            liq_dir = 1.0 if "UP" in features.trajectory.value else -1.0
            flow_signals.append(liq_dir * 0.35)

        if supporting is not None and supporting.healthy:
            basis = supporting.price / features.current_price - 1.0
            if abs(basis) > 0.001:
                inflow_signal = "exchange inflow pressure" if basis < 0 else "exchange outflow pressure"
                reasons.append(inflow_signal)
                flow_signals.append(-0.2 if basis < 0 else 0.2)

        flow_direction = math.tanh(sum(flow_signals)) if flow_signals else 0.0
        confidence_boost = min(0.15, abs(flow_direction) * 0.15) if flow_signals else 0.0

        return FlowAssessment(
            whale_detected=whale_detected,
            liquidity_removal=liquidity_removal,
            exchange_stress=exchange_stress,
            funding_spike=funding_spike,
            liquidation_cluster=liquidation_cluster,
            flow_direction=flow_direction,
            confidence_boost=confidence_boost,
            reasons=tuple(reasons) if reasons else ("no institutional flow signals",),
        )
