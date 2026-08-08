"""Main bot loop — scan mispricing + cross-venue arb, execute per risk rules."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table

from kalshi_bot.config import AppConfig, Settings
from kalshi_bot.data.ibit_options import BRTIProxy, IBITOptionsProvider
from kalshi_bot.execution.engine import ExecutionEngine, ExecutionReport
from kalshi_bot.execution.risk import RiskManager
from kalshi_bot.strategies.cross_venue_arb import CrossVenueArbScanner
from kalshi_bot.strategies.mispricing import MispricingScanner
from kalshi_bot.venues.kalshi import KalshiClient
from kalshi_bot.venues.polymarket import PolymarketClient

logger = logging.getLogger(__name__)
console = Console()


@dataclass
class BotStats:
    loops: int = 0
    signals_seen: int = 0
    trades: int = 0
    arbs: int = 0
    reports: list[ExecutionReport] = field(default_factory=list)


class TradingBot:
    def __init__(self, config: AppConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self.kalshi = KalshiClient(
            base_url=settings.kalshi_url,
            api_key_id=settings.kalshi_api_key_id,
            private_key_path=settings.kalshi_private_key_path,
        )
        self.poly = PolymarketClient()
        self.options = IBITOptionsProvider(
            cache_sec=config.pricing.smile_cache_sec,
            default_iv=config.pricing.default_iv,
        )
        self.brti = BRTIProxy(self.options)
        self.risk = RiskManager(config, max_daily_loss=settings.max_daily_loss_usd)
        self.engine = ExecutionEngine(self.kalshi, self.risk, config)
        self.mispricing = MispricingScanner(self.kalshi, self.options, self.brti, config)
        self.arb = CrossVenueArbScanner(self.kalshi, self.poly, config.cross_venue)
        self.stats = BotStats()

    def close(self) -> None:
        self.kalshi.close()
        self.poly.close()

    def once(self) -> BotStats:
        self.stats.loops += 1
        # --- Mispricing ---
        try:
            scan = self.mispricing.scan()
            self._print_mispricing(scan)
            for sig in scan.signals:
                self.stats.signals_seen += 1
                report = self.engine.execute_mispricing(sig)
                if report:
                    self.stats.trades += 1
                    self.stats.reports.append(report)
                    console.print(f"[green]{report.detail}[/green]")
        except Exception as exc:
            logger.exception("Mispricing scan failed: %s", exc)
            console.print(f"[red]Mispricing scan error: {exc}[/red]")

        # --- Cross-venue arb ---
        try:
            opps = self.arb.scan()
            if opps:
                table = Table(title="Cross-venue arb (Kalshi ↔ Polymarket)")
                table.add_column("Pair cost")
                table.add_column("Edge")
                table.add_column("Reason")
                for o in opps:
                    table.add_row(f"{o.pair_cost:.4f}", f"{o.edge:.4f}", o.reason)
                console.print(table)
            for o in opps:
                report = self.engine.execute_arb(o)
                if report:
                    self.stats.arbs += 1
                    self.stats.reports.append(report)
                    console.print(f"[cyan]{report.detail}[/cyan]")
            if not opps:
                console.print("[dim]No cross-venue arb this cycle[/dim]")
        except Exception as exc:
            logger.exception("Arb scan failed: %s", exc)
            console.print(f"[red]Arb scan error: {exc}[/red]")

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

    def _print_mispricing(self, scan) -> None:
        console.print(
            f"BTC(BRTI proxy)=${scan.spot:,.2f}  IBIT=${scan.ibit:.2f}  "
            f"ATM IV={((scan.iv_atm or 0)*100):.1f}%  scanned={scan.markets_scanned}"
        )
        if not scan.signals:
            console.print("[dim]No actionable mispricing signals[/dim]")
            return
        table = Table(title="Options-implied mispricing")
        table.add_column("Tier")
        table.add_column("Ticker")
        table.add_column("Side")
        table.add_column("Kalshi")
        table.add_column("Options")
        table.add_column("Edge pp")
        table.add_column("IV")
        table.add_column("Book $")
        for s in scan.signals[:15]:
            color = {
                "HIGH": "bold green",
                "MEDIUM": "yellow",
                "LOW": "white",
                "PASS": "dim",
            }.get(s.confidence.value, "white")
            table.add_row(
                f"[{color}]{s.confidence.value}[/{color}]",
                s.ticker,
                s.side.value,
                f"{s.kalshi_prob*100:.1f}%",
                f"{s.options_prob*100:.1f}%",
                f"{s.edge_pp:.1f}",
                f"{s.iv*100:.1f}%",
                f"{s.book_usd:.0f}",
            )
        console.print(table)