"""End-to-end active-market forecasting pipeline.

This module coordinates data and pure model components. It never places orders.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable

from kalshi_bot.config import AppConfig
from kalshi_bot.data.cf_benchmark import BenchmarkDataError, CFBenchmarkClient
from kalshi_bot.data.ibit_options import IBITOptionsProvider
from kalshi_bot.data.supporting_feeds import (
    InsufficientSupportingFeeds,
    SupportingFeeds,
)
from kalshi_bot.domain import (
    BenchmarkQuote,
    DecisionAction,
    DecisionResult,
    Direction,
    FeatureSnapshot,
    GateFailure,
    MarketPosition,
    MarketSnapshot,
    OpenOrder,
    ProbabilityEstimate,
    Regime,
    SupportingAggregate,
)
from kalshi_bot.features.engine import FeatureEngine
from kalshi_bot.market.discovery import DiscoveryConfig, MarketDiscovery
from kalshi_bot.models.ensemble import EnsembleProbabilityModel
from kalshi_bot.models.regime import classify_regime
from kalshi_bot.strategies.decision import DecisionConfig, DecisionEngine
from kalshi_bot.venues.kalshi import KalshiClient


@dataclass(frozen=True)
class ForecastCycle:
    timestamp: datetime
    data_health: str
    reason: str
    market: MarketSnapshot | None = None
    benchmark: BenchmarkQuote | None = None
    supporting: SupportingAggregate | None = None
    features: FeatureSnapshot | None = None
    regime: Regime | None = None
    forecast: ProbabilityEstimate | None = None
    decision: DecisionResult | None = None
    options_volatility: float | None = None
    market_rejections: dict[str, tuple[str, ...]] | None = None


PositionLookup = Callable[[str], MarketPosition | None]
OrdersLookup = Callable[[str], tuple[OpenOrder, ...]]


class ForecastingScanner:
    """Build one causal decision for the currently active KXBTC15M market."""

    def __init__(
        self,
        *,
        kalshi: KalshiClient,
        benchmark: CFBenchmarkClient,
        supporting: SupportingFeeds,
        options: IBITOptionsProvider,
        config: AppConfig,
        features: FeatureEngine | None = None,
        model: EnsembleProbabilityModel | None = None,
        decision_engine: DecisionEngine | None = None,
        position_lookup: PositionLookup | None = None,
        orders_lookup: OrdersLookup | None = None,
    ) -> None:
        self.kalshi = kalshi
        self.benchmark = benchmark
        self.supporting = supporting
        self.options = options
        self.config = config
        self.features = features or FeatureEngine()
        self.model = model or EnsembleProbabilityModel()
        self.discovery = MarketDiscovery(
            DiscoveryConfig(
                series_ticker="KXBTC15M",
                minimum_seconds_remaining=config.strategy.min_seconds_remaining,
                maximum_seconds_remaining=15 * 60,
                minimum_depth=config.strategy.order_quantity,
                maximum_spread=config.strategy.max_spread,
            )
        )
        self.decision_engine = decision_engine or DecisionEngine(
            DecisionConfig(
                minimum_edge=config.strategy.min_edge,
                target_edge=config.strategy.target_edge,
                quantity=config.strategy.order_quantity,
                maximum_benchmark_age=config.data.max_brti_age_seconds,
                minimum_seconds_remaining=config.strategy.min_seconds_remaining,
                minimum_confidence=config.strategy.min_confidence,
                minimum_agreement=config.strategy.min_signal_agreement,
                minimum_data_completeness=config.strategy.min_data_completeness,
                minimum_depth=config.strategy.order_quantity,
                maximum_spread=config.strategy.max_spread,
                fee_rate=config.execution.fee_rate,
                fee_per_contract=config.execution.fee_per_contract,
                slippage_bps=config.execution.slippage_bps,
                slippage_per_contract=config.execution.slippage_per_contract,
                late_seconds=config.strategy.late_seconds,
                late_minimum_edge=config.strategy.target_edge,
                final_seconds=config.strategy.final_seconds,
                final_minimum_edge=config.strategy.final_min_edge,
            )
        )
        self.position_lookup = position_lookup or (lambda _ticker: None)
        self.orders_lookup = orders_lookup or (lambda _ticker: ())

    @staticmethod
    def _no_trade(reason: str, gate: str, observed: object = None) -> DecisionResult:
        return DecisionResult(
            action=DecisionAction.NO_TRADE,
            reason=reason,
            gate_failures=(
                GateFailure(
                    gate=gate,
                    reason=reason,
                    observed=observed,
                    required="healthy, validated input",
                ),
            ),
            current_direction=Direction.FLAT,
            predicted_direction=Direction.FLAT,
            trade_direction=Direction.FLAT,
        )

    def _option_volatility(self, seconds_remaining: float) -> float | None:
        """Return an optional volatility prior; failures reduce evidence, never safety."""
        try:
            smile = self.options.nearest_smile(
                max(seconds_remaining, 1.0) / (365.25 * 24 * 3600)
            )
            return smile.atm_iv(self.config.pricing.default_iv) if smile else None
        except Exception:
            return None

    def scan(
        self,
        *,
        now: datetime | None = None,
        risk_locked: bool = False,
        duplicate_entry: bool = False,
    ) -> ForecastCycle:
        observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            markets = self.kalshi.get_markets("KXBTC15M", status="open", limit=20)
        except Exception as exc:
            reason = f"Kalshi market retrieval failed: {exc}"
            return ForecastCycle(
                observed_at,
                "FAILED",
                reason,
                decision=self._no_trade(reason, "kalshi_api", str(exc)),
            )

        orderbooks: dict[str, dict] = {}
        for market in markets:
            if market.status.lower() not in {"open", "active"}:
                continue
            if market.open_time and market.open_time > observed_at:
                continue
            try:
                orderbooks[market.ticker] = self.kalshi.get_orderbook(market.ticker, depth=50)
            except Exception:
                # Discovery records the missing/malformed book and fails closed.
                continue

        discovered = self.discovery.select(markets, orderbooks=orderbooks, now=observed_at)
        market = discovered.market
        if market is None:
            reason = "no valid active KXBTC15M BRTI contract"
            return ForecastCycle(
                observed_at,
                "FAILED",
                reason,
                decision=self._no_trade(reason, "market_discovery", discovered.rejections),
                market_rejections=dict(discovered.rejections),
            )
        market = replace(
            market,
            current_position=self.position_lookup(market.ticker),
            open_orders=self.orders_lookup(market.ticker),
        )

        try:
            benchmark = self.benchmark.get_quote(now=observed_at)
        except BenchmarkDataError as exc:
            reason = f"primary BRTI unavailable: {exc}"
            return ForecastCycle(
                observed_at,
                "FAILED",
                reason,
                market=market,
                decision=self._no_trade(reason, "primary_brti", str(exc)),
                market_rejections=dict(discovered.rejections),
            )
        self.features.add_quote(benchmark)

        supporting: SupportingAggregate | None = None
        supporting_reason = ""
        try:
            supporting = self.supporting.get_aggregate(now=observed_at)
            if supporting.dispersion > self.config.data.max_supporting_dispersion:
                supporting_reason = (
                    f"supporting venue dispersion {supporting.dispersion:.4%} exceeds limit"
                )
                supporting = None
        except InsufficientSupportingFeeds as exc:
            supporting_reason = str(exc)

        try:
            features = self.features.compute(market, now=observed_at, supporting=supporting)
        except ValueError as exc:
            reason = f"feature calculation failed: {exc}"
            return ForecastCycle(
                observed_at,
                "FAILED",
                reason,
                market=market,
                benchmark=benchmark,
                supporting=supporting,
                decision=self._no_trade(reason, "features", str(exc)),
            )

        regime = classify_regime(features)
        options_vol = self._option_volatility(features.seconds_remaining)
        market_prior = (
            (market.yes_bid + market.yes_ask) / 2
            if market.yes_bid is not None and market.yes_ask is not None
            else None
        )
        forecast = self.model.estimate(
            features,
            regime,
            options_volatility=options_vol,
            market_prior=market_prior,
        )
        decision = self.decision_engine.decide(
            market,
            forecast,
            features,
            benchmark,
            now=observed_at,
            risk_locked=risk_locked,
            duplicate_entry=duplicate_entry,
        )
        health = "DEGRADED" if supporting is None else "HEALTHY"
        reason = decision.reason
        if supporting_reason:
            reason = f"{reason}; supporting feeds: {supporting_reason}"
        return ForecastCycle(
            timestamp=observed_at,
            data_health=health,
            reason=reason,
            market=market,
            benchmark=benchmark,
            supporting=supporting,
            features=features,
            regime=regime,
            forecast=forecast,
            decision=decision,
            options_volatility=options_vol,
            market_rejections=dict(discovered.rejections),
        )
