"""End-to-end 1-hour forecasting pipeline."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from kalshi_bot.config import AppConfig
from kalshi_bot.data.cf_benchmark import (
    BenchmarkDataError,
    CFBenchmarkClient,
    KalshiCFBenchmarkClient,
    parse_kalshi_cfbenchmarks_history,
)
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
from kalshi_bot.hour.discovery import HourDiscoveryConfig, discover_all_hour_markets, discover_hour_market
from kalshi_bot.hour.mispricing import MispricingAssessment
from kalshi_bot.hour.strike_selection import (
    candidate_summary,
    rank_terminal_candidate,
    select_best_strike_candidate,
    StrikeCandidateResult,
)
from kalshi_bot.hour.feature_engine import HourFeatureBundle, HourFeatureEngine
from kalshi_bot.hour.probability_model import HourProbabilityModel, model_stability
from kalshi_bot.hour.regime_detector import classify_hour_regime
from kalshi_bot.hour.trajectory_model import TrajectoryForecast, forecast_trajectory
from kalshi_bot.hour.terminal_decision import (
    HourTerminalDecisionEngine,
    format_terminal_explanation,
    terminal_decision_config_from_app,
)
from kalshi_bot.hour.terminal_probability import TerminalForecast, TerminalProbabilityEngine
from kalshi_bot.hour.prediction_store import PredictionStore
from kalshi_bot.hour.trend_engine import TrendSnapshot
from kalshi_bot.intelligence.orchestrator import IntelligenceOrchestrator, IntelligenceReport
from kalshi_bot.strategies.decision import DecisionEngine, decision_config_from_app
from kalshi_bot.strategies.entry_filters import (
    EntrySignalTracker,
    apply_signal_persistence_gate,
    classify_window_regime,
)
from kalshi_bot.venues.kalshi import KalshiClient

if TYPE_CHECKING:
    from kalshi_bot.execution.risk import RiskManager


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
    terminal_forecast: TerminalForecast | None = None
    mispricing: MispricingAssessment | None = None
    terminal_explanation: str | None = None
    strike_candidates: tuple[dict, ...] | None = None

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
        decision_engine: DecisionEngine | None = None,
        intelligence: IntelligenceOrchestrator | None = None,
        position_lookup: PositionLookup | None = None,
        orders_lookup: OrdersLookup | None = None,
        prediction_store: PredictionStore | None = None,
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
                late_momentum_window_seconds=config.strategy.late_seconds,
            )
        )
        self.model = model or HourProbabilityModel(model_version=hour_cfg.model_version)
        self.discovery_config = HourDiscoveryConfig(
            hour=hour_cfg,
            minimum_depth=hour_cfg.order_quantity,
            maximum_spread=hour_cfg.max_spread,
        )
        self.decision_engine = decision_engine or DecisionEngine(
            decision_config_from_app(
                config,
                maximum_seconds_remaining=hour_cfg.max_entry_seconds_remaining,
            )
        )
        self.position_lookup = position_lookup or (lambda _ticker: None)
        self.orders_lookup = orders_lookup or (lambda _ticker: ())
        self.intelligence = intelligence or IntelligenceOrchestrator(
            confidence_threshold=config.strategy.min_confidence,
        )
        self.entry_tracker = EntrySignalTracker(
            required_polls=config.strategy.entry_signal_persistence_polls,
        )
        terminal_cfg = config.terminal_probability
        self.terminal_mode = terminal_cfg.enabled
        if self.terminal_mode:
            self.terminal_engine = TerminalProbabilityEngine(
                model=self.model,
                model_version=hour_cfg.model_version,
            )
            self.terminal_decision_engine = HourTerminalDecisionEngine(
                terminal_decision_config_from_app(config)
            )
            self.prediction_store = prediction_store or PredictionStore(
                terminal_cfg.predictions_db_path
            )
            self.entry_tracker = EntrySignalTracker(
                required_polls=terminal_cfg.signal_persistence_polls,
            )
            self._calibrator = None
        else:
            self.terminal_engine = None
            self.terminal_decision_engine = None
            self.prediction_store = None
            self._calibrator = None

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

    def _load_calibrator(self, observed_at: datetime):
        terminal_cfg = self.config.terminal_probability
        if not terminal_cfg.calibration.enabled or self.prediction_store is None:
            return None
        try:
            return self.prediction_store.build_calibrator(cutoff=observed_at)
        except Exception:
            return None

    def _scan_terminal_multi(
        self,
        *,
        observed_at: datetime,
        markets: list[MarketSnapshot],
        benchmark: BenchmarkQuote,
        supporting: SupportingAggregate | None,
        supporting_reason: str,
        discovered_rejections: dict,
        risk_locked: bool,
        duplicate_entry: bool,
        risk_manager: RiskManager | None,
    ) -> HourForecastCycle:
        terminal_cfg = self.config.terminal_probability
        calibrator = self._load_calibrator(observed_at)
        self._calibrator = calibrator

        if self.prediction_store is not None:
            for market in markets:
                self.prediction_store.resolve_expired(
                    now=observed_at,
                    settlement_brti=benchmark.price,
                    ticker=market.ticker,
                    strike=market.strike,
                )

        calibration_pass = True
        if terminal_cfg.calibration.enabled and self.prediction_store is not None:
            calibration_pass = self.prediction_store.calibration_pass(
                min_samples=terminal_cfg.calibration.min_samples_per_bucket,
                max_gap=terminal_cfg.calibration.max_calibration_gap,
            )

        evaluated: list[StrikeCandidateResult] = []
        candidate_rows: list[dict] = []

        for market in markets:
            try:
                bundle = self.features.compute_bundle(
                    market, now=observed_at, supporting=supporting
                )
            except ValueError:
                continue

            features = bundle.features
            trend = bundle.trend
            vol = bundle.volatility
            regime = classify_hour_regime(features, trend, vol)
            window_regime = (
                classify_window_regime(features)
                if self.config.strategy.window_regime_enabled
                else None
            )
            options_vol = self._option_volatility(features.seconds_remaining)
            market_prior = (
                (market.yes_bid + market.yes_ask) / 2
                if market.yes_bid is not None and market.yes_ask is not None
                else None
            )

            try:
                terminal = self.terminal_engine.estimate(
                    features,
                    regime,
                    trend,
                    vol,
                    market_strike=market.strike,
                    settlement_reference=market.reference or "CME CF BRTI",
                    options_volatility=options_vol,
                    market_prior=market_prior,
                    window_regime=window_regime,
                    calibrator=calibrator if calibrator and calibrator.fit_cutoff else None,
                )
            except ValueError:
                continue

            if benchmark.is_proxy:
                shrunk = 0.5 + (terminal.calibrated_p_yes - 0.5) * 0.80
                terminal = replace(
                    terminal,
                    calibrated_p_yes=shrunk,
                    calibrated_p_no=1.0 - shrunk,
                    confidence=terminal.confidence * 0.80,
                    notes=terminal.notes + (
                        "unofficial proxy: probability/confidence shrunk",
                    ),
                )

            decision, mispricing, stability_swing = self.terminal_decision_engine.decide(
                market,
                terminal,
                features,
                benchmark,
                now=observed_at,
                risk_locked=risk_locked,
                duplicate_entry=duplicate_entry,
                risk_manager=risk_manager,
                calibration_pass=calibration_pass,
                regime=regime,
            )

            has_position = (
                market.current_position is not None
                and market.current_position.quantity > 0
            )
            rank_key = rank_terminal_candidate(
                market=market,
                decision=decision,
                mispricing=mispricing,
                has_position=has_position,
            )
            summary = candidate_summary(market, decision, mispricing)
            evaluated.append(
                StrikeCandidateResult(
                    market=market,
                    terminal=terminal,
                    decision=decision,
                    mispricing=mispricing,
                    stability_swing=stability_swing,
                    rank_key=rank_key,
                    summary=summary,
                )
            )
            candidate_rows.append(
                {
                    "ticker": market.ticker,
                    "strike": market.strike,
                    "action": decision.action.value,
                    "edge": decision.edge,
                    "required_edge": mispricing.required_edge if mispricing else None,
                    "yes_net_edge": mispricing.yes_net_edge if mispricing else None,
                    "no_net_edge": mispricing.no_net_edge if mispricing else None,
                    "yes_ask": market.yes_ask,
                    "no_ask": market.no_ask,
                    "summary": summary,
                    "rank_key": rank_key,
                    "gate_count": len(decision.gate_failures),
                }
            )

            if self.prediction_store is not None:
                self.prediction_store.record(
                    timestamp=observed_at,
                    ticker=market.ticker,
                    strike=terminal.strike,
                    expiration=market.expiration,
                    brti_price=terminal.current_brti,
                    seconds_remaining=terminal.seconds_remaining,
                    predicted_p_yes=terminal.raw_p_yes,
                    calibrated_p_yes=terminal.calibrated_p_yes,
                    market_yes_ask=market.yes_ask,
                    market_no_ask=market.no_ask,
                    yes_net_edge=mispricing.yes_net_edge if mispricing else None,
                    no_net_edge=mispricing.no_net_edge if mispricing else None,
                    volatility=features.realized_vol,
                    regime=regime.value if regime else None,
                    confidence=terminal.confidence,
                    signal_agreement=terminal.signal_agreement,
                    action=decision.action.value,
                    payload={"summary": summary, "rank_key": rank_key},
                )

        best = select_best_strike_candidate(evaluated)
        if best is None:
            reason = "no evaluable strike candidates for active hourly contract"
            return HourForecastCycle(
                observed_at,
                "FAILED",
                reason,
                benchmark=benchmark,
                supporting=supporting,
                decision=self._no_trade(reason, "strike_selection", discovered_rejections),
                market_rejections=discovered_rejections,
                strike_candidates=tuple(candidate_rows),
            )

        market = best.market
        terminal = best.terminal
        decision = best.decision
        mispricing = best.mispricing
        stability_swing = best.stability_swing

        decision = apply_signal_persistence_gate(
            decision,
            ticker=market.ticker,
            tracker=self.entry_tracker,
        )

        try:
            bundle = self.features.compute_bundle(
                market, now=observed_at, supporting=supporting
            )
            features = bundle.features
            trend = bundle.trend
            trajectory = forecast_trajectory(
                current_price=features.current_price,
                strike=features.strike,
                seconds_remaining=features.seconds_remaining,
                trend=trend,
                realized_vol=features.realized_vol,
                trajectory=features.trajectory,
                z_distance=features.z_distance_to_strike,
            )
            regime = classify_hour_regime(features, trend, bundle.volatility)
        except ValueError:
            bundle = None
            features = None
            trend = None
            trajectory = None
            regime = None

        forecast = terminal.as_probability_estimate(regime=regime)
        stability = model_stability(forecast)
        liquidity_pass = not any(f.gate == "liquidity" for f in decision.gate_failures)
        explanation = format_terminal_explanation(
            terminal,
            mispricing,
            decision,
            calibration_pass=calibration_pass,
            liquidity_pass=liquidity_pass,
            stability_swing=stability_swing,
        )
        selection_note = (
            f"Selected {best.summary} from {len(evaluated)} strike candidates"
        )
        explanation = f"{selection_note}\n{explanation}"

        health = (
            "PROXY"
            if benchmark.is_proxy
            else "DEGRADED"
            if supporting is None
            else "HEALTHY"
        )
        reason = f"{decision.reason}; {selection_note}\n{explanation}"
        if benchmark.is_proxy:
            reason = f"{reason}; unofficial constituent proxy (PAPER only)"
        if supporting_reason:
            reason = f"{reason}; supporting feeds: {supporting_reason}"

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
            forecast=forecast,
            decision=decision,
            options_volatility=self._option_volatility(
                features.seconds_remaining if features else 0
            ),
            market_rejections=discovered_rejections,
            intelligence=None,
            model_stability=stability,
            terminal_forecast=terminal,
            mispricing=mispricing,
            terminal_explanation=explanation,
            strike_candidates=tuple(candidate_rows),
        )

    def _scan_terminal(
        self,
        *,
        observed_at: datetime,
        market: MarketSnapshot,
        benchmark: BenchmarkQuote,
        supporting: SupportingAggregate | None,
        supporting_reason: str,
        bundle: HourFeatureBundle,
        features: FeatureSnapshot,
        trend: TrendSnapshot,
        vol,
        regime: Regime,
        trajectory: TrajectoryForecast,
        options_vol: float | None,
        market_prior: float | None,
        window_regime,
        discovered_rejections: dict,
        risk_locked: bool,
        duplicate_entry: bool,
        risk_manager: RiskManager | None,
    ) -> HourForecastCycle:
        terminal_cfg = self.config.terminal_probability
        calibrator = self._load_calibrator(observed_at)
        self._calibrator = calibrator

        if self.prediction_store is not None:
            self.prediction_store.resolve_expired(
                now=observed_at,
                settlement_brti=benchmark.price,
                ticker=market.ticker,
                strike=market.strike,
            )

        try:
            terminal = self.terminal_engine.estimate(
                features,
                regime,
                trend,
                vol,
                market_strike=market.strike,
                settlement_reference=market.reference or "CME CF BRTI",
                options_volatility=options_vol,
                market_prior=market_prior,
                window_regime=window_regime,
                calibrator=calibrator if calibrator and calibrator.fit_cutoff else None,
            )
        except ValueError as exc:
            reason = f"terminal probability failed: {exc}"
            return HourForecastCycle(
                observed_at,
                "FAILED",
                reason,
                market=market,
                benchmark=benchmark,
                supporting=supporting,
                bundle=bundle,
                trend=trend,
                trajectory=trajectory,
                regime=regime,
                decision=self._no_trade(reason, "terminal_probability", str(exc)),
                market_rejections=discovered_rejections,
            )

        if benchmark.is_proxy:
            shrunk = 0.5 + (terminal.calibrated_p_yes - 0.5) * 0.80
            terminal = replace(
                terminal,
                calibrated_p_yes=shrunk,
                calibrated_p_no=1.0 - shrunk,
                confidence=terminal.confidence * 0.80,
                notes=terminal.notes + ("unofficial proxy: probability/confidence shrunk",),
            )

        calibration_pass = True
        if terminal_cfg.calibration.enabled and self.prediction_store is not None:
            calibration_pass = self.prediction_store.calibration_pass(
                min_samples=terminal_cfg.calibration.min_samples_per_bucket,
                max_gap=terminal_cfg.calibration.max_calibration_gap,
            )

        decision, mispricing, stability_swing = self.terminal_decision_engine.decide(
            market,
            terminal,
            features,
            benchmark,
            now=observed_at,
            risk_locked=risk_locked,
            duplicate_entry=duplicate_entry,
            risk_manager=risk_manager,
            calibration_pass=calibration_pass,
            regime=regime,
        )
        decision = apply_signal_persistence_gate(
            decision,
            ticker=market.ticker,
            tracker=self.entry_tracker,
        )

        forecast = terminal.as_probability_estimate()
        stability = model_stability(forecast)
        liquidity_pass = not any(
            f.gate == "liquidity" for f in decision.gate_failures
        )
        explanation = format_terminal_explanation(
            terminal,
            mispricing,
            decision,
            calibration_pass=calibration_pass,
            liquidity_pass=liquidity_pass,
            stability_swing=stability_swing,
        )

        if self.prediction_store is not None:
            self.prediction_store.record(
                timestamp=observed_at,
                ticker=market.ticker,
                strike=terminal.strike,
                expiration=market.expiration,
                brti_price=terminal.current_brti,
                seconds_remaining=terminal.seconds_remaining,
                predicted_p_yes=terminal.raw_p_yes,
                calibrated_p_yes=terminal.calibrated_p_yes,
                market_yes_ask=market.yes_ask,
                market_no_ask=market.no_ask,
                yes_net_edge=mispricing.yes_net_edge if mispricing else None,
                no_net_edge=mispricing.no_net_edge if mispricing else None,
                volatility=features.realized_vol,
                regime=regime.value if regime else None,
                confidence=terminal.confidence,
                signal_agreement=terminal.signal_agreement,
                action=decision.action.value,
                payload={
                    "explanation": explanation,
                    "required_edge": mispricing.required_edge if mispricing else None,
                    "expected_terminal_brti": terminal.expected_terminal_brti,
                    "terminal_volatility": terminal.terminal_volatility,
                    "normalized_strike_distance": terminal.normalized_strike_distance,
                },
            )

        health = (
            "PROXY"
            if benchmark.is_proxy
            else "DEGRADED"
            if supporting is None
            else "HEALTHY"
        )
        reason = f"{decision.reason}\n{explanation}"
        if benchmark.is_proxy:
            reason = f"{reason}; unofficial constituent proxy (PAPER only)"
        if supporting_reason:
            reason = f"{reason}; supporting feeds: {supporting_reason}"

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
            forecast=forecast,
            decision=decision,
            options_volatility=options_vol,
            market_rejections=discovered_rejections,
            intelligence=None,
            model_stability=stability,
            terminal_forecast=terminal,
            mispricing=mispricing,
            terminal_explanation=explanation,
        )

    def scan(
        self,
        *,
        now: datetime | None = None,
        risk_locked: bool = False,
        duplicate_entry: bool = False,
        risk_manager: RiskManager | None = None,
    ) -> HourForecastCycle:
        observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        series = self.config.hour.series_ticker
        try:
            markets = self.kalshi.get_markets(series, status="open", limit=200)
        except Exception as exc:
            reason = f"Kalshi market retrieval failed: {exc}"
            return HourForecastCycle(
                observed_at,
                "FAILED",
                reason,
                decision=self._no_trade(reason, "kalshi_api", str(exc)),
            )

        reference_price: float | None = None
        try:
            reference_price = self.benchmark.get_quote(now=observed_at).price
        except Exception:
            reference_price = None

        from kalshi_bot.hour.discovery import filter_hourly_markets, select_nearest_strike_markets

        hourly_markets = filter_hourly_markets(markets, config=self.discovery_config)
        candidate_markets = hourly_markets
        if reference_price is not None and hourly_markets:
            candidate_markets = select_nearest_strike_markets(
                hourly_markets,
                reference_price,
                count=10,
            )

        orderbooks: dict[str, dict] = {}
        for market in candidate_markets:
            if market.status.lower() not in {"open", "active"}:
                continue
            if market.open_time and market.open_time > observed_at:
                continue
            try:
                orderbooks[market.ticker] = self.kalshi.get_orderbook(market.ticker, depth=50)
            except Exception:
                continue

        discovered = discover_hour_market(
            candidate_markets,
            orderbooks=orderbooks,
            now=observed_at,
            config=self.discovery_config,
            reference_price=reference_price,
        )
        discovery_batch = discover_all_hour_markets(
            candidate_markets,
            orderbooks=orderbooks,
            now=observed_at,
            config=self.discovery_config,
            reference_price=reference_price,
            strike_count=10,
        )
        candidate_markets_snapshots = list(discovery_batch.markets)
        market = discovered.market
        if self.terminal_mode and candidate_markets_snapshots:
            market = candidate_markets_snapshots[0]
        elif market is None and candidate_markets_snapshots:
            market = candidate_markets_snapshots[0]
        if market is None:
            reason = f"no valid active {series} 1-hour BRTI contract"
            return HourForecastCycle(
                observed_at,
                "FAILED",
                reason,
                decision=self._no_trade(reason, "market_discovery", discovery_batch.rejections),
                market_rejections=dict(discovery_batch.rejections),
            )

        if self.terminal_mode and candidate_markets_snapshots:
            for idx, snap in enumerate(candidate_markets_snapshots):
                candidate_markets_snapshots[idx] = replace(
                    snap,
                    current_position=self.position_lookup(snap.ticker),
                    open_orders=self.orders_lookup(snap.ticker),
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
                    market_rejections=dict(discovery_batch.rejections),
                )
            if isinstance(self.benchmark, KalshiCFBenchmarkClient):
                try:
                    raw = self.benchmark.kalshi.get(
                        "/cfbenchmarks/values",
                        params={"id": self.benchmark.index_id},
                    )
                    envelope = raw.get("data", raw) if isinstance(raw, dict) else raw
                    history = parse_kalshi_cfbenchmarks_history(
                        envelope,
                        now=observed_at,
                        history_seconds=self.features.config.history_seconds,
                    )
                    if history:
                        self.features.add_quotes(history)
                    else:
                        self.features.add_quote(benchmark)
                except Exception:
                    self.features.add_quote(benchmark)
            else:
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

            return self._scan_terminal_multi(
                observed_at=observed_at,
                markets=candidate_markets_snapshots,
                benchmark=benchmark,
                supporting=supporting,
                supporting_reason=supporting_reason,
                discovered_rejections=dict(discovery_batch.rejections),
                risk_locked=risk_locked,
                duplicate_entry=duplicate_entry,
                risk_manager=risk_manager,
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
        if isinstance(self.benchmark, KalshiCFBenchmarkClient):
            try:
                raw = self.benchmark.kalshi.get(
                    "/cfbenchmarks/values",
                    params={"id": self.benchmark.index_id},
                )
                envelope = raw.get("data", raw) if isinstance(raw, dict) else raw
                history = parse_kalshi_cfbenchmarks_history(
                    envelope,
                    now=observed_at,
                    history_seconds=self.features.config.history_seconds,
                )
                if history:
                    self.features.add_quotes(history)
                else:
                    self.features.add_quote(benchmark)
            except Exception:
                self.features.add_quote(benchmark)
        else:
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
        window_regime = (
            classify_window_regime(features)
            if self.config.strategy.window_regime_enabled
            else None
        )
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
            window_regime=window_regime,
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
        intel_report: IntelligenceReport | None = None
        trade_forecast = forecast
        intel_skip = False
        if self.config.intelligence.enabled:
            intel_report = self.intelligence.enrich(
                forecast,
                features,
                market,
                regime,
                supporting=supporting,
            )
            trade_forecast = intel_report.adjusted_forecast or forecast
            intel_skip = intel_report.skip_trade

        decision = self.decision_engine.decide(
            market,
            trade_forecast,
            features,
            benchmark,
            now=observed_at,
            risk_locked=risk_locked or intel_skip,
            duplicate_entry=duplicate_entry,
            risk_manager=risk_manager,
        )
        decision = apply_signal_persistence_gate(
            decision,
            ticker=market.ticker,
            tracker=self.entry_tracker,
        )
        if (
            intel_skip
            and self.config.intelligence.enabled
            and decision.action in {
                DecisionAction.BUY_UP,
                DecisionAction.BUY_DOWN,
            }
        ):
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
        if intel_report is not None and intel_report.skip_trade:
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
