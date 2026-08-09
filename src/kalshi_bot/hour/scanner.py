"""End-to-end 1-hour forecasting pipeline."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable

from kalshi_bot.config import AppConfig
from kalshi_bot.data.cf_benchmark import BenchmarkDataError, CFBenchmarkClient
from kalshi_bot.data.ibit_options import IBITOptionsProvider
from kalshi_bot.data.supporting_feeds import (
    ConstituentBRTIProxy,
    InsufficientSupportingFeeds,
    SupportingFeedError,
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
from kalshi_bot.features.engine import FeatureEngineConfig
from kalshi_bot.hour.decision import HourDecisionConfig, HourDecisionEngine
from kalshi_bot.hour.discovery import HourDiscoveryConfig, discover_hour_market
from kalshi_bot.hour.feature_engine import HourFeatureBundle, HourFeatureEngine
from kalshi_bot.hour.probability_model import HourProbabilityModel, model_stability
from kalshi_bot.hour.regime_detector import classify_hour_regime
from kalshi_bot.hour.trajectory_model import TrajectoryForecast, forecast_trajectory
from kalshi_bot.hour.trend_engine import TrendSnapshot
from kalshi_bot.intelligence.orchestrator import IntelligenceOrchestrator, IntelligenceReport
from kalshi_bot.venues.kalshi import KalshiClient


@dataclass(frozen=True)
class HourForecastCycle:
    timestamp: datetime
    data_health: str
    reason: str
    market: MarketSnapshot | None = None
    benchmark: BenchmarkQuote | None = None
    supporting: SupportingAggregate | None = None
    bundle: HourFeatureBundle | None = None
    trend: TrendSnapshot | None = None
    trajectory: TrajectoryForecast | None = None
    regime: Regime | None = None
    forecast: ProbabilityEstimate | None = None
    decision: DecisionResult | None = None
    options_volatility: float | None = None
    market_rejections: dict[str, tuple[str, ...]] | None = None
    intelligence: IntelligenceReport | None = None
    model_stability: float | None = None

    @property
    def features(self) -> FeatureSnapshot | None:
        return self.bundle.features if self.bundle else None


PositionLookup = Callable[[str], MarketPosition | None]
OrdersLookup = Callable[[str], tuple[OpenOrder, ...]]


class HourForecastingScanner:
    """Build one causal decision for the active 1-hour KXBTCD contract."""

    def __init__(
        self,
        *,
        kalshi: KalshiClient,
        benchmark: CFBenchmarkClient | ConstituentBRTIProxy,
        supporting: SupportingFeeds,
        options: IBITOptionsProvider,
        config: AppConfig,
        features: HourFeatureEngine | None = None,
        model: HourProbabilityModel | None = None,
        decision_engine: HourDecisionEngine | None = None,
        intelligence: IntelligenceOrchestrator | None = None,
        position_lookup: PositionLookup | None = None,
        orders_lookup: OrdersLookup | None = None,
    ) -> None:
        self.kalshi = kalshi
        self.benchmark = benchmark
        self.supporting = supporting
        self.options = options
        self.config = config
        hour_cfg = config.hour
        self.features = features or HourFeatureEngine(
            FeatureEngineConfig(
                history_seconds=hour_cfg.history_seconds,
                allow_proxy=config.data.benchmark_mode == "constituent_proxy",
            )
        )
        self.model = model or HourProbabilityModel(model_version=hour_cfg.model_version)
        self.discovery_config = HourDiscoveryConfig(
            hour=hour_cfg,
            minimum_depth=hour_cfg.order_quantity,
            maximum_spread=hour_cfg.max_spread,
        )
        self.decision_engine = decision_engine or HourDecisionEngine(
            HourDecisionConfig(
                hour=hour_cfg,
                edge=config.hour_edge,
                maximum_benchmark_age=config.data.max_brti_age_seconds,
                fee_rate=config.execution.fee_rate,
                fee_per_contract=config.execution.fee_per_contract,
                slippage_bps=config.execution.slippage_bps,
                slippage_per_contract=config.execution.slippage_per_contract,
                allow_proxy_data=(
                    config.execution.dry_run
                    and config.data.benchmark_mode == "constituent_proxy"
                ),
                proxy_minimum_constituents=config.data.min_supporting_venues,
                proxy_maximum_dispersion=config.data.max_supporting_dispersion,
            )
        )
        self.position_lookup = position_lookup or (lambda _ticker: None)
        self.orders_lookup = orders_lookup or (lambda _ticker: ())
        self.intelligence = intelligence or IntelligenceOrchestrator(
            confidence_threshold=hour_cfg.min_confidence,
        )

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
    ) -> HourForecastCycle:
        observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        series = self.config.hour.series_ticker
        try:
            markets = self.kalshi.get_markets(series, status="open", limit=40)
        except Exception as exc:
            reason = f"Kalshi market retrieval failed: {exc}"
            return HourForecastCycle(
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
                continue

        discovered = discover_hour_market(
            markets,
            orderbooks=orderbooks,
            now=observed_at,
            config=self.discovery_config,
        )
        market = discovered.market
        if market is None:
            reason = f"no valid active {series} 1-hour BRTI contract"
            return HourForecastCycle(
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
        except (BenchmarkDataError, SupportingFeedError) as exc:
            reason = f"primary BRTI unavailable: {exc}"
            return HourForecastCycle(
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
        if benchmark.is_proxy and isinstance(self.benchmark, ConstituentBRTIProxy):
            supporting = self.benchmark.last_aggregate
        else:
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
            bundle = self.features.compute_bundle(
                market, now=observed_at, supporting=supporting
            )
        except ValueError as exc:
            reason = f"feature calculation failed: {exc}"
            return HourForecastCycle(
                observed_at,
                "FAILED",
                reason,
                market=market,
                benchmark=benchmark,
                supporting=supporting,
                decision=self._no_trade(reason, "features", str(exc)),
            )

        features = bundle.features
        trend = bundle.trend
        vol = bundle.volatility
        regime = classify_hour_regime(features, trend, vol)
        trajectory = forecast_trajectory(
            current_price=features.current_price,
            strike=features.strike,
            seconds_remaining=features.seconds_remaining,
            trend=trend,
            realized_vol=features.realized_vol,
            trajectory=features.trajectory,
            z_distance=features.z_distance_to_strike,
        )
        options_vol = self._option_volatility(features.seconds_remaining)
        market_prior = (
            (market.yes_bid + market.yes_ask) / 2
            if market.yes_bid is not None and market.yes_ask is not None
            else None
        )
        forecast = self.model.estimate(
            features,
            regime,
            trend,
            vol,
            options_volatility=options_vol,
            market_prior=market_prior,
        )
        if benchmark.is_proxy:
            proxy_p_up = 0.5 + (forecast.p_up - 0.5) * 0.80
            forecast = replace(
                forecast,
                p_up=proxy_p_up,
                p_down=1.0 - proxy_p_up,
                confidence=forecast.confidence * 0.80,
                notes=forecast.notes + (
                    "unofficial constituent proxy: probability/confidence shrunk",
                ),
            )

        stability = model_stability(forecast)
        intel_report = self.intelligence.enrich(
            forecast,
            features,
            market,
            regime,
            supporting=supporting,
        )
        trade_forecast = intel_report.adjusted_forecast or forecast

        decision = self.decision_engine.decide(
            market,
            trade_forecast,
            features,
            benchmark,
            trend,
            vol,
            regime,
            stability,
            now=observed_at,
            risk_locked=risk_locked or intel_report.skip_trade,
            duplicate_entry=duplicate_entry,
        )
        if intel_report.skip_trade and decision.action in {
            DecisionAction.BUY_UP,
            DecisionAction.BUY_DOWN,
        }:
            decision = replace(
                decision,
                action=DecisionAction.NO_TRADE,
                reason=f"intelligence gate: {intel_report.skip_reason}",
                gate_failures=decision.gate_failures + (
                    GateFailure(
                        gate="intelligence",
                        reason=intel_report.skip_reason,
                        observed=trade_forecast.confidence,
                        required=self.config.hour.min_confidence,
                    ),
                ),
            )

        health = (
            "PROXY"
            if benchmark.is_proxy
            else "DEGRADED"
            if supporting is None
            else "HEALTHY"
        )
        reason = decision.reason
        if benchmark.is_proxy:
            reason = f"{reason}; unofficial constituent proxy (PAPER only)"
        if supporting_reason:
            reason = f"{reason}; supporting feeds: {supporting_reason}"
        if intel_report.skip_trade:
            reason = f"{reason}; {intel_report.skip_reason}"

        return HourForecastCycle(
            timestamp=observed_at,
            data_health=health,
            reason=reason,
            market=market,
            benchmark=benchmark,
            supporting=supporting,
            bundle=bundle,
            trend=trend,
            trajectory=trajectory,
            regime=regime,
            forecast=trade_forecast,
            decision=decision,
            options_volatility=options_vol,
            market_rejections=dict(discovered.rejections),
            intelligence=intel_report,
            model_stability=stability,
        )
