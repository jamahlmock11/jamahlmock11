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
from kalshi_bot.data.external import ExternalDataProvider
from kalshi_bot.features.engine import FeatureEngine, FeatureEngineConfig
from kalshi_bot.features.enriched import EnrichedFeatureEngine, EnrichedFeatures
from kalshi_bot.intelligence.model_agreement import ModelAgreementAssessment, assess_model_agreement
from kalshi_bot.intelligence.orchestrator import IntelligenceOrchestrator, IntelligenceReport
from kalshi_bot.intelligence.trade_quality import TradeQualityAssessment, assess_trade_quality
from kalshi_bot.learning.pattern_matcher import PatternMatcher, PatternMatchResult
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
    enriched: EnrichedFeatures | None = None
    regime: Regime | None = None
    forecast: ProbabilityEstimate | None = None
    decision: DecisionResult | None = None
    options_volatility: float | None = None
    market_rejections: dict[str, tuple[str, ...]] | None = None
    intelligence: IntelligenceReport | None = None
    model_agreement: ModelAgreementAssessment | None = None
    pattern_match: PatternMatchResult | None = None
    trade_quality: TradeQualityAssessment | None = None


PositionLookup = Callable[[str], MarketPosition | None]
OrdersLookup = Callable[[str], tuple[OpenOrder, ...]]


class ForecastingScanner:
    """Build one causal decision for the currently active KXBTC15M market."""

    def __init__(
        self,
        *,
        kalshi: KalshiClient,
        benchmark: CFBenchmarkClient | ConstituentBRTIProxy,
        supporting: SupportingFeeds,
        options: IBITOptionsProvider,
        config: AppConfig,
        features: FeatureEngine | None = None,
        model: EnsembleProbabilityModel | None = None,
        decision_engine: DecisionEngine | None = None,
        intelligence: IntelligenceOrchestrator | None = None,
        position_lookup: PositionLookup | None = None,
        orders_lookup: OrdersLookup | None = None,
    ) -> None:
        self.kalshi = kalshi
        self.benchmark = benchmark
        self.supporting = supporting
        self.options = options
        self.config = config
        self.features = features or FeatureEngine(
            FeatureEngineConfig(
                allow_proxy=config.data.benchmark_mode == "constituent_proxy"
            )
        )
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
                allow_proxy_data=(
                    config.execution.dry_run
                    and config.data.benchmark_mode == "constituent_proxy"
                ),
                proxy_minimum_constituents=config.data.min_supporting_venues,
                proxy_maximum_dispersion=config.data.max_supporting_dispersion,
                stop_loss_fraction=config.risk.stop_loss_fraction,
                opposite_edge_shift=config.risk.opposite_edge_shift,
                thesis_reversal_margin=config.risk.thesis_reversal_margin,
                thesis_reversal_enabled=config.risk.thesis_reversal_enabled,
                opposite_edge_exit_enabled=config.risk.opposite_edge_exit_enabled,
                recovery_hold_enabled=config.risk.recovery_hold_enabled,
                recovery_hold_min_probability=config.risk.recovery_hold_min_probability,
                recovery_hold_min_confidence=config.risk.recovery_hold_min_confidence,
                recovery_hold_min_agreement=config.risk.recovery_hold_min_agreement,
                min_hold_seconds=config.risk.min_hold_seconds,
            )
        )
        self.position_lookup = position_lookup or (lambda _ticker: None)
        self.orders_lookup = orders_lookup or (lambda _ticker: ())
        self.intelligence = intelligence or IntelligenceOrchestrator(
            confidence_threshold=config.strategy.min_confidence,
        )
        self.enriched_engine = EnrichedFeatureEngine()
        self.pattern_matcher = PatternMatcher()
        self.external_data = ExternalDataProvider(
            enabled=config.strategy.external_data_enabled,
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
        except (BenchmarkDataError, SupportingFeedError) as exc:
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
        enriched = self.enriched_engine.compute(
            features, market, regime, now=observed_at
        )
        external = self.external_data.fetch(observed_at)
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
        if benchmark.is_proxy:
            # Basis uncertainty is represented by shrinking both probability
            # and confidence before any edge comparison.
            proxy_p_up = 0.5 + (forecast.p_up - 0.5) * 0.80
            forecast = replace(
                forecast,
                p_up=proxy_p_up,
                p_down=1.0 - proxy_p_up,
                confidence=forecast.confidence * 0.80,
                notes=forecast.notes
                + (
                    "unofficial constituent proxy: probability/confidence shrunk",
                ),
            )

        intel_report = self.intelligence.enrich(
            forecast,
            features,
            market,
            regime,
            supporting=supporting,
        )
        trade_forecast = intel_report.adjusted_forecast or forecast

        model_agreement = assess_model_agreement(
            trade_forecast,
            features,
            enriched,
            regime,
            min_agreement=self.config.strategy.min_signal_agreement,
        )
        pattern_match = self.pattern_matcher.match(features, enriched, regime)

        decision = self.decision_engine.decide(
            market,
            trade_forecast,
            features,
            benchmark,
            now=observed_at,
            risk_locked=risk_locked or intel_report.skip_trade,
            duplicate_entry=duplicate_entry,
        )
        trade_quality = assess_trade_quality(
            forecast=trade_forecast,
            features=features,
            market=market,
            enriched=enriched,
            model_agreement=model_agreement,
            pattern_match=pattern_match,
            edge=decision.edge,
            regime=regime,
            min_quality_score=self.config.strategy.min_trade_quality_score,
            max_dnt_score=self.config.strategy.max_do_not_trade_score,
        )
        if (
            self.config.strategy.require_trade_quality
            and decision.action in {DecisionAction.BUY_UP, DecisionAction.BUY_DOWN}
            and not trade_quality.should_execute
        ):
            skip_reason = (
                f"trade quality {trade_quality.trade_quality_score:.0f}/100 "
                f"(DNT {trade_quality.do_not_trade_score:.0f}); "
                f"{', '.join(trade_quality.reasons) or 'mediocre opportunity'}"
            )
            decision = replace(
                decision,
                action=DecisionAction.NO_TRADE,
                reason=f"trade quality gate: {skip_reason}",
                gate_failures=decision.gate_failures + (
                    GateFailure(
                        gate="trade_quality",
                        reason=skip_reason,
                        observed=trade_quality.trade_quality_score,
                        required=self.config.strategy.min_trade_quality_score,
                    ),
                ),
                trade_tier=trade_quality.trade_tier,
                size_multiplier=0.0,
            )
        elif decision.action in {DecisionAction.BUY_UP, DecisionAction.BUY_DOWN}:
            decision = replace(
                decision,
                trade_tier=trade_quality.trade_tier,
                size_multiplier=trade_quality.size_multiplier,
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
                        required=self.config.strategy.min_confidence,
                    ),
                ),
            )

        intel_report = self.intelligence.enrich(
            trade_forecast,
            features,
            market,
            regime,
            decision_action=decision.action.value,
            decision_edge=decision.edge,
            supporting=supporting,
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
        if external.uncertainty_score > 0.7:
            reason = f"{reason}; elevated external uncertainty"
        if pattern_match and not pattern_match.similar_setup_found:
            reason = f"{reason}; {pattern_match.recommendation}"
        elif pattern_match and pattern_match.match_count < self.config.strategy.min_pattern_matches:
            reason = f"{reason}; {pattern_match.recommendation}"
        return ForecastCycle(
            timestamp=observed_at,
            data_health=health,
            reason=reason,
            market=market,
            benchmark=benchmark,
            supporting=supporting,
            features=features,
            enriched=enriched,
            regime=regime,
            forecast=trade_forecast,
            decision=decision,
            options_volatility=options_vol,
            market_rejections=dict(discovered.rejections),
            intelligence=intel_report,
            model_agreement=model_agreement,
            pattern_match=pattern_match,
            trade_quality=trade_quality,
        )
