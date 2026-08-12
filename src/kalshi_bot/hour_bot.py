"""1-hour Kalshi BTC trading loop."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from kalshi_bot.bot import BotStats
from kalshi_bot.config import AppConfig, Settings
from kalshi_bot.data.cf_benchmark import create_benchmark_feed
from kalshi_bot.data.ibit_options import IBITOptionsProvider
from kalshi_bot.data.supporting_feeds import SupportingFeeds
from kalshi_bot.domain import ContractSide, DecisionAction, MarketPosition, OpenOrder
from kalshi_bot.execution.engine import ExecutionEngine, ExecutionReport
from kalshi_bot.execution.position_manager import PositionManager, PositionManagerConfig
from kalshi_bot.execution.position_reversal import (
    evaluate_position_reversal,
    reversal_config_from_risk,
)
from kalshi_bot.execution.risk import RiskManager
from kalshi_bot.intelligence.kill_switch import ConfidenceKillSwitch
from kalshi_bot.intelligence.orchestrator import IntelligenceOrchestrator
from kalshi_bot.journal import TradeJournal
from kalshi_bot.journal_payload import (
    decision_execution_snapshot,
    strategy_config_snapshot,
    strike_context_snapshot,
)
from kalshi_bot.learning.signal_weights import SignalWeightTracker
from kalshi_bot.hour.scanner import HourForecastCycle, HourForecastingScanner
from kalshi_bot.strategies.decision import format_edge_gap
from kalshi_bot.venues.kalshi import KalshiClient

logger = logging.getLogger(__name__)
console = Console()


class HourTradingBot:
    def __init__(
        self,
        config: AppConfig,
        settings: Settings,
        journal: TradeJournal | None = None,
    ):
        self.config = config
        self.settings = settings
        self.journal = journal or TradeJournal()
        signal_weights_path = Path("data/signal_weights_hour.json")
        self.signal_weights = (
            SignalWeightTracker.load(signal_weights_path)
            if signal_weights_path.exists()
            else SignalWeightTracker()
        )
        self.kill_switch = ConfidenceKillSwitch()
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
        self.forecasting = HourForecastingScanner(
            kalshi=self.kalshi,
            benchmark=self.benchmark,
            supporting=self.supporting,
            options=self.options,
            config=config,
            position_lookup=self._position_lookup,
            orders_lookup=self._orders_lookup,
            intelligence=self.intelligence,
        )
        self.stats = BotStats()

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
        except Exception as exc:
            self.risk.lock(f"position verification failed: {exc}")
            logger.error("Could not hydrate hour bot positions: %s", exc)

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
        self.kalshi.close()
        self.benchmark.close()
        self.supporting.close()

    def once(self) -> BotStats:
        self.stats.loops += 1
        self.risk.begin_cycle()
        mode = "DRY-RUN" if self.engine.dry_run else "LIVE"
        cycle = self.forecasting.scan(
            risk_locked=self.risk.locked,
            risk_manager=self.risk,
        )
        report = None
        if (
            self.config.execution.orders_enabled
            and cycle.market is not None
            and cycle.decision is not None
        ):
            report = self.engine.execute_decision(
                cycle.market,
                cycle.decision,
                timestamp=cycle.timestamp,
                benchmark=cycle.benchmark,
            )
        traded = bool(report and report.ok)
        position_reversal = None
        if (
            cycle.market is not None
            and cycle.market.current_position is not None
            and cycle.market.current_position.quantity > 0
            and cycle.features is not None
            and cycle.forecast is not None
            and self.config.risk.position_reversal_enabled
        ):
            reversal = evaluate_position_reversal(
                position_side=cycle.market.current_position.side,
                features=cycle.features,
                forecast=cycle.forecast,
                cfg=reversal_config_from_risk(self.config.risk),
            )
            position_reversal = {
                "should_reverse": reversal.should_reverse,
                "summary": reversal.summary,
                "reason": reversal.reason,
            }
        journal_payload: dict = {
            "horizon": "1h",
            "model_version": self.config.hour.model_version,
            "execution": report.payload if report else None,
            "position_reversal": position_reversal,
            **decision_execution_snapshot(cycle.decision),
            "config": strategy_config_snapshot(self.config, horizon="1h"),
            "risk": {
                "locked": self.risk.locked,
                "reason": self.risk.state.halt_reason,
                "realized_pnl": self.risk.state.realized_pnl,
                "open_exposure_usd": self.risk.state.open_exposure_usd,
            },
        }
        if cycle.features is not None:
            journal_payload["strike_context"] = strike_context_snapshot(cycle.features)
        self.journal.log_decision(
            cycle,
            dry_run=self.engine.dry_run,
            traded=traded,
            payload=journal_payload,
        )
        self.stats.decisions += 1
        if traded:
            self.stats.trades += 1
        else:
            self.stats.no_trades += 1
        if report:
            self.stats.reports.append(report)
        self._print_cycle(cycle, mode)
        if report:
            color = "green" if report.ok else "red"
            console.print(f"[{color}]{report.detail}[/{color}]")
        return self.stats

    def run_forever(self) -> None:
        interval = self.config.hour.poll_interval_sec
        mode = "DRY-RUN" if self.engine.dry_run else "LIVE"
        console.print(
            f"[bold]Kalshi BTC 1-hour bot starting[/bold] mode={mode} "
            f"series={self.config.hour.series_ticker} poll={interval}s"
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

    def _print_cycle(self, cycle: HourForecastCycle, mode: str) -> None:
        decision = cycle.decision
        table = Table(title=f"Kalshi BTC 1h forecast · {mode}")
        table.add_column("Field")
        table.add_column("Value")
        table.add_row("Data health", cycle.data_health)
        if cycle.market:
            table.add_row("Ticker", cycle.market.ticker)
            table.add_row("Strike", f"${cycle.market.strike:,.2f}")
            table.add_row(
                "Time remaining",
                f"{max(0, (cycle.market.expiration - cycle.timestamp).total_seconds()) / 60:.1f}m",
            )
            table.add_row(
                "Book",
                f"YES {cycle.market.yes_bid:.2f}/{cycle.market.yes_ask:.2f} · "
                f"NO {cycle.market.no_bid:.2f}/{cycle.market.no_ask:.2f}",
            )
        if cycle.benchmark:
            label = "Unofficial proxy" if cycle.benchmark.is_proxy else "Primary BRTI"
            table.add_row(label, f"${cycle.benchmark.price:,.2f}")
        if cycle.trend:
            table.add_row("Trend", cycle.trend.classification.value)
            table.add_row(
                "Trend consistency",
                f"{cycle.trend.trend_consistency:.0%}",
            )
        if cycle.trajectory:
            table.add_row(
                "Current dir",
                cycle.trajectory.current_direction.value,
            )
            table.add_row(
                "Expected expiration",
                cycle.trajectory.expected_expiration_direction.value,
            )
        if cycle.forecast:
            table.add_row(
                "Probability",
                f"UP {cycle.forecast.p_up:.1%} · DOWN {cycle.forecast.p_down:.1%}",
            )
            table.add_row(
                "Confidence",
                f"{cycle.forecast.confidence:.1%} · agreement {cycle.forecast.signal_agreement:.1%}",
            )
        if cycle.regime:
            table.add_row("Regime", cycle.regime.value)
        if decision:
            table.add_row("Decision", decision.action.value)
            if decision.edge is not None:
                table.add_row("Edge", f"{decision.edge:.1%}")
            if decision.required_edge is not None:
                table.add_row("Required edge", f"{decision.required_edge:.1%}")
            table.add_row("Edge gap", format_edge_gap(decision))
            if decision.trade_tier is not None and decision.trade_tier.value != "NONE":
                table.add_row("Trade tier", decision.trade_tier.value)
            if decision.entry_timing:
                table.add_row("Entry timing", decision.entry_timing.value)
            table.add_row("Why", cycle.reason)
        console.print(table)
