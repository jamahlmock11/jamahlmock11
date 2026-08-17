"""Main settlement-aware forecasting, decision, and execution loop."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from kalshi_bot.config import AppConfig, Settings
from kalshi_bot.data.cf_benchmark import create_benchmark_feed
from kalshi_bot.data.ibit_options import IBITOptionsProvider
from kalshi_bot.data.spot_hub import SpotPriceHub
from kalshi_bot.data.supporting_feeds import SupportingFeeds
from kalshi_bot.domain import ContractSide, DecisionAction, MarketPosition, OpenOrder, Regime
from kalshi_bot.execution.engine import ExecutionEngine, ExecutionReport
from kalshi_bot.execution.position_reversal import (
    evaluate_position_reversal,
    reversal_config_from_risk,
)
from kalshi_bot.execution.position_manager import PositionManager, PositionManagerConfig
from kalshi_bot.execution.risk import RiskManager
from kalshi_bot.intelligence.kill_switch import ConfidenceKillSwitch
from kalshi_bot.intelligence.orchestrator import IntelligenceOrchestrator
from kalshi_bot.journal import TradeJournal
from kalshi_bot.learning.signal_weights import SignalWeightTracker
from kalshi_bot.learning.trade_recorder import TradeRecorder
from kalshi_bot.learning.pattern_matcher import PatternMatcher, feature_vector_dict
from kalshi_bot.learning.outcomes import record_round_trip_learning
from kalshi_bot.models.strike_gravity import assess_strike_gravity
from kalshi_bot.strategies.alt_runner import AltStrategyRunner
from kalshi_bot.strategies.forecasting import ForecastCycle, ForecastingScanner
from kalshi_bot.strategies.decision import format_edge_gap
from kalshi_bot.strategies.lag_reversal import ReversalContextTracker, evaluate_lag_reversal
from kalshi_bot.strategies.reversal_score import ReversalScoreAssessment, ReversalTier
from kalshi_bot.strategies.forecast_setup import ForecastSetupAssessment
from kalshi_bot.agents.pipeline import RomaPipeline, format_roma_report
from kalshi_bot.venues.kalshi import KalshiClient

logger = logging.getLogger(__name__)
console = Console()


@dataclass
class BotStats:
    loops: int = 0
    decisions: int = 0
    trades: int = 0
    no_trades: int = 0
    reports: list[ExecutionReport] = field(default_factory=list)


class TradingBot:
    def __init__(
        self,
        config: AppConfig,
        settings: Settings,
        journal: TradeJournal | None = None,
    ):
        self.config = config
        self.settings = settings
        self.journal = journal or TradeJournal()
        signal_weights_path = Path("data/signal_weights.json")
        self.signal_weights_path = signal_weights_path
        self.signal_weights = (
            SignalWeightTracker.load(signal_weights_path)
            if signal_weights_path.exists()
            else SignalWeightTracker()
        )
        self.trade_recorder = TradeRecorder()
        self.pattern_matcher = PatternMatcher()
        self.kill_switch = ConfidenceKillSwitch()
        self._hydrate_kill_switch()
        self.intelligence = IntelligenceOrchestrator(
            kill_switch=self.kill_switch,
            signal_weights=self.signal_weights,
            confidence_threshold=config.strategy.min_confidence,
        )
        self.kalshi = KalshiClient(
            base_url=settings.kalshi_url,
            api_key_id=settings.kalshi_api_key_id,
            private_key_path=settings.kalshi_private_key_path,
        )
        self.options = IBITOptionsProvider(
            cache_sec=config.pricing.smile_cache_sec,
            default_iv=config.pricing.default_iv,
        )
        self.supporting = SupportingFeeds(
            minimum_venues=config.data.min_supporting_venues
        )
        self.benchmark = create_benchmark_feed(
            config=config,
            kalshi=self.kalshi,
            supporting=self.supporting,
        )
        self.positions = PositionManager(
            PositionManagerConfig(
                max_flips_per_contract=config.risk.max_flips_per_contract,
                max_trades_per_contract=config.risk.max_trades_per_contract,
            ),
            mode="paper" if config.execution.dry_run else "live",
        )
        self.risk = RiskManager(
            config,
            max_daily_loss=config.risk.max_daily_loss,
            cooldown_sec=config.risk.cooldown_seconds,
            max_trades_per_cycle=1,
            max_per_ticker_usd=config.risk.max_contract_exposure,
            hard_min_edge=(
                config.longshot.min_edge
                if config.longshot.enabled
                else config.strategy.min_edge
            ),
        )
        self._hydrate_positions()
        self.engine = ExecutionEngine(
            self.kalshi,
            self.risk,
            config,
            journal=self.journal,
            positions=self.positions,
        )
        self.forecasting = ForecastingScanner(
            kalshi=self.kalshi,
            benchmark=self.benchmark,
            supporting=self.supporting,
            options=self.options,
            config=config,
            position_lookup=self._position_lookup,
            orders_lookup=self._orders_lookup,
            intelligence=self.intelligence,
            pattern_matcher=self.pattern_matcher,
        )
        self.spot_hub = SpotPriceHub(
            poll_interval_sec=config.spot_lag.poll_interval_sec,
        )
        if config.spot_lag.enabled:
            self.spot_hub.start()
        self.alt_runner = AltStrategyRunner(config, self.spot_hub)
        self.reversal_tracker = ReversalContextTracker()
        self.roma = RomaPipeline(config.agents)
        self.stats = BotStats()

    def _hydrate_kill_switch(self) -> None:
        """Load recent prediction outcomes into the kill switch."""
        try:
            decisions = self.journal.recent_decisions(limit=50)
            outcomes: list[bool] = []
            for row in reversed(decisions):
                predicted = row.get("predicted_direction")
                trade_dir = row.get("trade_direction")
                outcome = row.get("outcome")
                if outcome is None or trade_dir not in ("UP", "DOWN"):
                    continue
                actual_up = float(outcome) >= 0.5
                predicted_up = trade_dir == "UP" if trade_dir in ("UP", "DOWN") else predicted == "UP"
                outcomes.append(predicted_up == actual_up)
            if outcomes:
                self.kill_switch.hydrate(outcomes)
        except Exception as exc:
            logger.warning("Could not hydrate kill switch: %s", exc)

    def _maybe_update_signal_weights(self) -> None:
        """Run nightly signal weight update when UTC hour is 0."""
        now = datetime.now(timezone.utc)
        if now.hour == 0 and now.minute < 5:
            path = Path("data/signal_weights.json")
            self.signal_weights.update_weights()
            self.signal_weights.save(path)

    def _hydrate_positions(self) -> None:
        if not self.kalshi.authenticated:
            return
        try:
            mkts = self.kalshi.get_positions(limit=200)
            self.risk.seed_from_positions(mkts)
            now = datetime.now(timezone.utc)
            for raw in mkts:
                ticker = str(raw.get("ticker") or "")
                position = float(raw.get("position_fp") or raw.get("position") or 0)
                if not ticker or position == 0:
                    continue
                quantity = abs(position)
                exposure = abs(float(raw.get("market_exposure_dollars") or 0))
                average_price = exposure / quantity if exposure > 0 else 0.5
                self.positions.seed_position(
                    contract=ticker,
                    side=ContractSide.YES if position > 0 else ContractSide.NO,
                    quantity=quantity,
                    entry_price=max(0.0, min(1.0, average_price)),
                    timestamp=now,
                )
            logger.info(
                "Hydrated %d positions, exposure=$%.2f",
                len(self.risk.state.positions),
                self.risk.state.open_exposure_usd,
            )
        except Exception as exc:
            self.risk.lock(f"position verification failed: {exc}")
            logger.error("Could not safely hydrate positions: %s", exc)

    def _position_lookup(self, ticker: str) -> MarketPosition | None:
        position = self.positions.position(ticker)
        if position is None:
            return None
        return MarketPosition(
            side=position.side,
            quantity=position.quantity,
            average_price=position.entry_price,
            opened_at=position.opened_at,
        )

    def _orders_lookup(self, ticker: str) -> tuple[OpenOrder, ...]:
        if not self.kalshi.authenticated:
            return ()
        try:
            raw_orders = self.kalshi.get_open_orders(ticker=ticker)
            orders: list[OpenOrder] = []
            for raw in raw_orders:
                raw_side = str(raw.get("side") or raw.get("outcome") or "yes").lower()
                side = ContractSide.NO if raw_side == "no" else ContractSide.YES
                price = float(
                    raw.get("yes_price_dollars")
                    or raw.get("no_price_dollars")
                    or raw.get("price")
                    or 0
                )
                orders.append(
                    OpenOrder(
                        order_id=str(raw.get("order_id") or raw.get("client_order_id") or "unknown"),
                        side=side,
                        quantity=float(raw.get("remaining_count_fp") or raw.get("count") or 0),
                        price=price,
                        status=str(raw.get("status") or "open"),
                    )
                )
            return tuple(orders)
        except Exception as exc:
            logger.error("Open-order verification failed: %s", exc)
            return (
                OpenOrder(
                    order_id="verification-failed",
                    side=ContractSide.YES,
                    quantity=0,
                    price=0,
                    status="pending",
                ),
            )

    def close(self) -> None:
        self.spot_hub.close()
        self.kalshi.close()
        self.benchmark.close()
        self.supporting.close()

    def once(self) -> BotStats:
        self._maybe_update_signal_weights()
        self.stats.loops += 1
        self.risk.begin_cycle()
        mode = "DRY-RUN" if self.engine.dry_run else "LIVE"
        cycle = self.forecasting.scan(
            risk_locked=self.risk.locked,
            risk_manager=self.risk,
        )
        alt_reports: list[ExecutionReport] = []
        lag_eval = None
        if (
            cycle.market is not None
            and cycle.features is not None
            and cycle.enriched is not None
            and cycle.forecast is not None
            and cycle.regime is not None
            and self.config.lag_reversal.enabled
        ):
            seconds_remaining = max(
                0.0, (cycle.market.expiration - cycle.timestamp).total_seconds()
            )
            lag_eval = evaluate_lag_reversal(
                cycle.market,
                features=cycle.features,
                enriched=cycle.enriched,
                forecast=cycle.forecast,
                regime=cycle.regime,
                cfg=self.config.lag_reversal,
                seconds_remaining=seconds_remaining,
                tracker=self.reversal_tracker,
                sweet_spot_min_seconds=self.config.strategy.entry_sweet_spot_min_seconds,
                sweet_spot_max_seconds=self.config.strategy.entry_sweet_spot_max_seconds,
            )
            cycle = replace(cycle, reversal_assessment=lag_eval.assessment)
            if (
                lag_eval.signal is not None
                and self.config.lag_reversal.entry_enabled
                and self.config.execution.orders_enabled
            ):
                lag_report = self.engine.execute_alt_signal(lag_eval.signal)
                if lag_report:
                    alt_reports.append(lag_report)
                    console.print(f"[cyan]{lag_report.detail}[/cyan]")
        if (
            self.config.execution.orders_enabled
            and cycle.market is not None
            and (
                self.config.spot_lag.enabled
                or self.config.orderbook_skew.enabled
                or self.config.mean_reversion.enabled
            )
        ):
            seconds_remaining = max(
                0.0, (cycle.market.expiration - cycle.timestamp).total_seconds()
            )
            spot_price = cycle.benchmark.price if cycle.benchmark else None
            alt = self.alt_runner.evaluate(
                cycle.market,
                seconds_remaining=seconds_remaining,
                position=cycle.market.current_position,
                open_orders=cycle.market.open_orders,
                spot_price=spot_price,
            )
            for signal in alt.signals:
                alt_report = self.engine.execute_alt_signal(signal)
                if alt_report:
                    alt_reports.append(alt_report)
                    console.print(
                        f"[cyan]{alt_report.detail}[/cyan]"
                    )
        report = None
        forecast_entry = (
            cycle.decision is not None
            and cycle.decision.action in {DecisionAction.BUY_UP, DecisionAction.BUY_DOWN}
        )
        skip_forecast_entry = (
            self.config.lag_reversal.enabled
            and self.config.lag_reversal.entry_enabled
            and self.config.lag_reversal.suppress_forecast_entries
            and forecast_entry
        )
        if (
            self.config.execution.orders_enabled
            and cycle.market is not None
            and cycle.decision is not None
            and not skip_forecast_entry
        ):
            exit_side = None
            if cycle.decision.action is DecisionAction.EXIT and cycle.market.current_position:
                exit_side = cycle.market.current_position.side
            report = self.engine.execute_decision(
                cycle.market,
                cycle.decision,
                timestamp=cycle.timestamp,
                benchmark=cycle.benchmark,
            )
            if (
                report
                and report.ok
                and report.realized_pnl is not None
                and exit_side is not None
                and cycle.market is not None
            ):
                record_round_trip_learning(
                    ticker=cycle.market.ticker,
                    held_side=exit_side,
                    pnl=report.realized_pnl,
                    trade_recorder=self.trade_recorder,
                    pattern_matcher=self.pattern_matcher,
                    journal=self.journal,
                    signal_weights=self.signal_weights,
                    signal_weights_path=self.signal_weights_path,
                    features=cycle.features,
                    market=cycle.market,
                    component_probabilities=(
                        cycle.forecast.component_probabilities if cycle.forecast else None
                    ),
                )
        traded = bool(report and report.ok) or any(r.ok for r in alt_reports)
        payload: dict = {
            "execution": report.payload if report else None,
            "config": {
                "min_edge": self.config.strategy.min_edge,
                "min_seconds_remaining": self.config.strategy.min_seconds_remaining,
                "max_entry_seconds_remaining": self.config.strategy.max_entry_seconds_remaining,
                "min_signal_agreement": self.config.strategy.min_signal_agreement,
                "min_data_completeness": self.config.strategy.min_data_completeness,
                "late_confidence_increment": self.config.strategy.late_confidence_increment,
                "min_entry_executable_cost": self.config.strategy.min_entry_executable_cost,
                "max_spread": self.config.strategy.max_spread,
                "min_confidence": self.config.strategy.min_confidence,
                "late_seconds": self.config.strategy.late_seconds,
                "late_favorite_seconds": self.config.strategy.late_favorite_seconds,
                "late_favorite_poll_threshold": self.config.strategy.late_favorite_poll_threshold,
                "late_favorite_min_edge": self.config.strategy.late_favorite_min_edge,
                "minimum_dominant_poll": self.config.strategy.minimum_dominant_poll,
                "min_trade_quality_score": self.config.strategy.min_trade_quality_score,
                "kelly_fraction": self.config.risk.kelly_fraction,
                "min_hold_seconds": self.config.risk.min_hold_seconds,
            },
            "risk": {
                "locked": self.risk.locked,
                "reason": self.risk.state.halt_reason,
                "realized_pnl": self.risk.state.realized_pnl,
                "open_exposure_usd": self.risk.state.open_exposure_usd,
                "consecutive_losses": self.risk.state.consecutive_losses,
            },
            "horizon": "15m",
            "strategy": (
                "lag_reversal"
                if self.config.lag_reversal.enabled and self.config.lag_reversal.entry_enabled
                else ("longshot" if self.config.longshot.enabled else "forecast")
            ),
        }
        if cycle.trade_quality is not None:
            tq = cycle.trade_quality
            payload["trade_quality"] = {
                "score": tq.trade_quality_score,
                "do_not_trade_score": tq.do_not_trade_score,
                "recommendation": tq.recommendation,
                "liquidity_label": tq.liquidity_label,
                "historical_match_count": tq.historical_match_count,
                "trade_tier": tq.trade_tier.value,
            }
        if cycle.model_agreement is not None:
            payload["model_agreement"] = {
                "agreement": cycle.model_agreement.agreement,
                "consensus": cycle.model_agreement.consensus_direction,
                "models_agree": cycle.model_agreement.models_agree,
            }
        if cycle.enriched is not None:
            payload["enriched_features"] = cycle.enriched.as_dict()
            if cycle.features is not None and cycle.regime is not None:
                payload["entry_features"] = feature_vector_dict(
                    cycle.features,
                    cycle.enriched,
                    cycle.regime,
                )
            else:
                payload["entry_features"] = cycle.enriched.as_dict().get("price_action", {})
        if cycle.features is not None:
            payload["strike_context"] = {
                "seconds_remaining": cycle.features.seconds_remaining,
                "spot": cycle.features.current_price,
                "strike": (
                    cycle.features.settlement_effective_strike
                    if cycle.features.settlement_effective_strike is not None
                    else cycle.features.strike
                ),
                "z_distance": cycle.features.z_distance_to_strike,
                "hold_up_probability": assess_strike_gravity(cycle.features).finish_probability_up,
                "late_momentum_pattern": cycle.features.late_momentum_pattern,
                "late_momentum_summary": cycle.features.late_momentum_summary,
            }
        payload["lag_reversal"] = {
            "enabled": self.config.lag_reversal.enabled,
            "entry_enabled": self.config.lag_reversal.entry_enabled,
            "signal_only": (
                self.config.lag_reversal.enabled
                and not self.config.lag_reversal.entry_enabled
            ),
            "min_entry_score": self.config.lag_reversal.min_entry_score,
            "rationale": lag_eval.rationale if lag_eval is not None else None,
        }
        if cycle.reversal_assessment is not None:
            ra = cycle.reversal_assessment
            if isinstance(ra, ReversalScoreAssessment):
                payload["reversal_signal"] = {
                    "score": ra.score,
                    "tier": ra.tier.value,
                    "active": ra.tier is not ReversalTier.NONE,
                    "initial_direction": ra.initial_direction,
                    "reversal_side": ra.reversal_side.value,
                    "kalshi_lag": ra.kalshi_lag_on_reversal_side,
                    "probability_change": ra.probability_change,
                    "summary": ra.summary,
                    "setup": (
                        f"{ra.initial_direction} stall → {ra.reversal_side.value} · "
                        f"lag {ra.kalshi_lag_on_reversal_side:+.1%} · "
                        f"Δprob {ra.probability_change:+.1%}"
                    ),
                }
        self.journal.log_decision(
            cycle,
            dry_run=self.engine.dry_run,
            traded=traded,
            payload=payload,
        )
        if (
            traded
            and cycle.market is not None
            and cycle.decision is not None
            and cycle.enriched is not None
            and cycle.forecast is not None
            and cycle.features is not None
            and cycle.regime is not None
            and cycle.decision.action in {DecisionAction.BUY_UP, DecisionAction.BUY_DOWN}
        ):
            self.trade_recorder.record_entry(
                ticker=cycle.market.ticker,
                features=cycle.enriched.as_dict(),
                prediction=cycle.forecast.p_up,
                confidence=cycle.forecast.confidence,
                edge=cycle.decision.edge or 0.0,
                action=cycle.decision.action.value,
                reason=cycle.decision.reason,
            )
            self.pattern_matcher.save_entry(
                cycle.features,
                cycle.enriched,
                cycle.regime,
                ticker=cycle.market.ticker,
                prediction=cycle.forecast.p_up,
                confidence=cycle.forecast.confidence,
                edge=cycle.decision.edge or 0.0,
                action=cycle.decision.action.value,
            )
        self.stats.decisions += 1
        if traded:
            self.stats.trades += 1
        else:
            self.stats.no_trades += 1
        if report:
            self.stats.reports.append(report)
        self._print_cycle(cycle, mode)
        if not self.config.longshot.enabled and self.config.agents.enabled:
            roma = self.roma.evaluate(cycle, risk_locked=self.risk.locked)
            if roma is not None:
                console.print(f"\n[bold cyan]{format_roma_report(roma)}[/bold cyan]")
        if report:
            color = "green" if report.ok else "red"
            console.print(f"[{color}]{report.detail}[/{color}]")

        return self.stats

    def run_forever(self) -> None:
        interval = self.config.execution.poll_interval_sec
        mode = "DRY-RUN" if self.engine.dry_run else "LIVE"
        console.print(
            f"[bold]Kalshi BTC bot starting[/bold] mode={mode} "
            f"series={self.config.series} poll={interval}s"
        )
        try:
            while True:
                if self.risk.state.halted:
                    console.print(f"[red]HALTED: {self.risk.state.halt_reason}[/red]")
                    break
                self.once()
                time.sleep(interval)
        except KeyboardInterrupt:
            console.print("\n[yellow]Stopped by user[/yellow]")
        finally:
            self.close()

    def _print_cycle(self, cycle: ForecastCycle, mode: str) -> None:
        decision = cycle.decision
        table = Table(title=f"Kalshi BTC 15m forecast · {mode}")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("Data health", cycle.data_health)
        if cycle.market:
            table.add_row("Ticker", cycle.market.ticker)
            table.add_row("Strike", f"${cycle.market.strike:,.2f}")
            table.add_row(
                "Time remaining",
                f"{max(0, (cycle.market.expiration-cycle.timestamp).total_seconds()):.0f}s",
            )
            table.add_row(
                "Book",
                f"YES {cycle.market.yes_bid:.2f}/{cycle.market.yes_ask:.2f} · "
                f"NO {cycle.market.no_bid:.2f}/{cycle.market.no_ask:.2f}",
            )
        if cycle.benchmark:
            label = (
                "Unofficial proxy"
                if cycle.benchmark.is_proxy
                else "Primary BRTI"
            )
            table.add_row(label, f"${cycle.benchmark.price:,.2f}")
        if cycle.features and cycle.market:
            strike = (
                cycle.features.settlement_effective_strike
                if cycle.features.settlement_effective_strike is not None
                else cycle.features.strike
            )
            distance = cycle.features.current_price - strike
            direction = "above" if distance >= 0 else "below"
            table.add_row(
                "Spot vs strike",
                (
                    f"${cycle.features.current_price:,.2f} vs ${strike:,.2f} "
                    f"({distance:+,.0f} · {distance / max(strike, 1.0):+.2%} {direction})"
                ),
            )
            table.add_row(
                "Strike distance",
                f"{cycle.features.z_distance_to_strike:+.2f}σ · "
                f"{cycle.features.seconds_remaining:.0f}s left",
            )
            gravity = assess_strike_gravity(cycle.features)
            hold_side = "UP" if gravity.finish_probability_up >= 0.5 else "DOWN"
            hold_prob = max(
                gravity.finish_probability_up,
                1.0 - gravity.finish_probability_up,
            )
            table.add_row(
                "Path hold",
                f"{hold_side} {hold_prob:.0%}",
            )
            if cycle.features.late_momentum_pattern != "none":
                table.add_row(
                    "Late momentum",
                    cycle.features.late_momentum_summary,
                )
            elif cycle.features.seconds_remaining <= self.config.strategy.late_seconds:
                table.add_row(
                    "Late momentum",
                    (
                        f"scanning · drift {cycle.features.late_momentum_drift:.2f} · "
                        f"hammer {cycle.features.late_momentum_hammer:.2f} · "
                        f"fade {cycle.features.late_momentum_fade:.2f}"
                    ),
                )
            position = (
                cycle.market.current_position
                if cycle.market and cycle.market.current_position
                else None
            )
            if (
                position is not None
                and position.quantity > 0
                and cycle.forecast is not None
                and self.config.risk.position_reversal_enabled
            ):
                reversal = evaluate_position_reversal(
                    position_side=position.side,
                    features=cycle.features,
                    forecast=cycle.forecast,
                    cfg=reversal_config_from_risk(self.config.risk),
                )
                label = "Reversal risk" if reversal.should_reverse else "Position hold"
                table.add_row(label, reversal.summary)
        if not self.config.longshot.enabled:
            if cycle.forecast:
                table.add_row(
                    "Probability",
                    f"UP {cycle.forecast.p_up:.1%} · DOWN {cycle.forecast.p_down:.1%}",
                )
                core = cycle.forecast.component_probabilities.get("brti_settlement_core")
                if core is not None:
                    pattern = cycle.features.late_momentum_pattern
                    pattern_label = (
                        f" · {pattern}" if pattern != "none" else ""
                    )
                    table.add_row(
                        "BRTI settlement core",
                        f"UP {core:.1%}{pattern_label} (spot/strike · momentum · vol · time)",
                    )
                table.add_row(
                    "Confidence",
                    f"{cycle.forecast.confidence:.1%} · agreement {cycle.forecast.signal_agreement:.1%}",
                )
            if cycle.regime:
                table.add_row("Regime", cycle.regime.value)
            if self.config.intelligence.enabled and cycle.intelligence:
                intel = cycle.intelligence
                table.add_row(
                    "Monte Carlo",
                    f"UP {intel.monte_carlo.p_up:.1%} · DOWN {intel.monte_carlo.p_down:.1%}",
                )
                table.add_row("Trading regime", intel.trading_regime.label)
                if intel.kill_switch.halted:
                    table.add_row("Kill switch", f"ACTIVE: {intel.kill_switch.reason}")
        if cycle.setup_assessment is not None:
            sa = cycle.setup_assessment
            if isinstance(sa, ForecastSetupAssessment):
                sweet = "sweet-spot" if sa.in_sweet_spot else "off-spot"
                table.add_row(
                    "Setup score",
                    f"{sa.score:.0f}/100 · {sa.initial_direction} · {sweet}",
                )
                comp = sa.components
                table.add_row(
                    "Setup inputs",
                    (
                        f"mom {comp.momentum_exhaustion:.0%} · struct {comp.structure_break:.0%} · "
                        f"vol {comp.volume_confirmation:.0%} · flow {comp.order_flow_reversal:.0%} · "
                        f"volσ {comp.volatility_shift:.0%} · z {comp.distance_from_strike:.0%} · "
                        f"time {comp.time_remaining:.0%}"
                    ),
                )
        if cycle.reversal_assessment is not None:
            ra = cycle.reversal_assessment
            if isinstance(ra, ReversalScoreAssessment):
                label = (
                    "Reversal signal"
                    if self.config.lag_reversal.enabled
                    and not self.config.lag_reversal.entry_enabled
                    else "Reversal score"
                )
                table.add_row(label, f"{ra.score:.0f}/100 · {ra.tier.value}")
                table.add_row(
                    "Reversal setup",
                    f"{ra.initial_direction} stall → {ra.reversal_side.value} · "
                    f"lag {ra.kalshi_lag_on_reversal_side:+.1%} · Δprob {ra.probability_change:+.1%}",
                )
        if decision:
            table.add_row("Decision", decision.action.value)
            if decision.edge is not None:
                table.add_row("All-in edge", f"{decision.edge:.1%}")
            if decision.quantity > 0 and decision.action in {
                DecisionAction.BUY_UP,
                DecisionAction.BUY_DOWN,
            }:
                table.add_row("Kelly size", f"{int(decision.quantity)} contracts")
            table.add_row("Edge gap", format_edge_gap(decision))
            if cycle.trade_quality:
                tq = cycle.trade_quality
                table.add_row(
                    "Trade quality",
                    f"{tq.trade_quality_score:.0f}/100 · {tq.recommendation} · "
                    f"DNT {tq.do_not_trade_score:.0f}",
                )
                table.add_row(
                    "Liquidity",
                    f"{tq.liquidity_label} · tier {tq.trade_tier.value}",
                )
            if cycle.model_agreement:
                ma = cycle.model_agreement
                table.add_row(
                    "Model agreement",
                    f"{ma.agreement:.0%} {ma.consensus_direction} "
                    f"({len(ma.dissenting_models)} dissenting)",
                )
            table.add_row("Why", cycle.reason)
        if (
            self.config.intelligence.enabled
            and not self.config.longshot.enabled
            and cycle.intelligence
            and cycle.intelligence.explainability
        ):
            console.print(cycle.intelligence.explainability.format_report())
        console.print(table)