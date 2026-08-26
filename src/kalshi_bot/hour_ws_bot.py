"""1-hour Kalshi WebSocket + crowd-favorite trading bot."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from rich.console import Console

from kalshi_bot.config import AppConfig, Settings
from kalshi_bot.hour.discovery import HourDiscoveryConfig, filter_hourly_markets
from kalshi_bot.hour_ws.orderbook_state import LiveOrderbook
from kalshi_bot.hour_ws.strategy import CrowdFavoriteStrategy, StrategyResult
from kalshi_bot.journal import TradeJournal
from kalshi_bot.venues.kalshi import KalshiClient, KalshiMarket
from kalshi_bot.venues.kalshi_ws import KalshiWebSocketClient

logger = logging.getLogger(__name__)
console = Console()


@dataclass
class OpenPosition:
    side: str
    entry: float
    entry_time: datetime
    confidence: float


@dataclass
class HourWSBotStats:
    messages: int = 0
    evaluations: int = 0
    trades: int = 0
    holds: int = 0


@dataclass
class HourWSBot:
    config: AppConfig
    settings: Settings
    journal: TradeJournal | None = None
    kalshi: KalshiClient = field(init=False)
    ws: KalshiWebSocketClient = field(init=False)
    strategy: CrowdFavoriteStrategy = field(init=False)
    stats: HourWSBotStats = field(default_factory=HourWSBotStats)
    positions: dict[str, OpenPosition] = field(default_factory=dict)
    price_data: dict[str, deque[float]] = field(default_factory=dict)
    orderbooks: dict[str, LiveOrderbook] = field(default_factory=dict)
    volumes: dict[str, int] = field(default_factory=dict)
    market_meta: dict[str, KalshiMarket] = field(default_factory=dict)
    daily_pnl: float = 0.0
    _running: bool = False
    _market_tickers: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.journal = self.journal or TradeJournal("data/journal_1h_ws.db")
        self.kalshi = KalshiClient(
            base_url=self.settings.kalshi_url,
            api_key_id=self.settings.kalshi_api_key_id,
            private_key_path=self.settings.kalshi_private_key_path,
        )
        self.ws = KalshiWebSocketClient(self.kalshi)
        self.strategy = CrowdFavoriteStrategy(self.config.hour_ws)

    @property
    def dry_run(self) -> bool:
        return self.config.execution.dry_run

    @property
    def orders_enabled(self) -> bool:
        return self.config.execution.orders_enabled

    def close(self) -> None:
        self.kalshi.close()

    def refresh_markets(self) -> list[str]:
        ws_cfg = self.config.hour_ws
        raw_markets = self.kalshi.get_markets(ws_cfg.series_ticker, status="open", limit=200)
        filtered = filter_hourly_markets(
            raw_markets,
            config=HourDiscoveryConfig(hour=self.config.hour),
        )
        now = datetime.now(timezone.utc)
        tickers: list[str] = []
        self.market_meta.clear()
        for market in filtered:
            seconds = (market.close_time - now).total_seconds()
            if seconds < ws_cfg.min_seconds_remaining:
                continue
            if seconds > ws_cfg.max_entry_seconds_remaining:
                continue
            tickers.append(market.ticker)
            self.market_meta[market.ticker] = market
            self.price_data.setdefault(market.ticker, deque(maxlen=100))
            self.orderbooks.setdefault(market.ticker, LiveOrderbook())
            self.volumes.setdefault(market.ticker, int(market.volume))
        self._market_tickers = tickers
        return tickers

    def _mid_price(self, ticker: str) -> float | None:
        book = self.orderbooks.get(ticker)
        if book is not None:
            snapshot = book.to_snapshot()
            if snapshot is not None and snapshot.yes_ask is not None and snapshot.yes_bid is not None:
                return (snapshot.yes_bid + snapshot.yes_ask) / 2.0
        market = self.market_meta.get(ticker)
        if market is not None and market.yes_ask > 0 and market.yes_bid > 0:
            return (market.yes_bid + market.yes_ask) / 2.0
        if market is not None and market.yes_ask > 0:
            return market.yes_ask
        prices = self.price_data.get(ticker)
        if prices:
            return prices[-1]
        return None

    async def handle_message(self, data: dict[str, Any]) -> None:
        self.stats.messages += 1
        msg_type = data.get("type")
        msg = data.get("msg") or data.get("data") or {}

        if msg_type in {"orderbook_snapshot", "orderbook_delta", "ticker"}:
            ticker = str(
                msg.get("market_ticker")
                or msg.get("ticker")
                or data.get("market_ticker")
                or ""
            )
            if not ticker:
                return
            book = self.orderbooks.setdefault(ticker, LiveOrderbook())
            if msg_type == "orderbook_snapshot":
                book.load_snapshot(msg)
            elif msg_type == "orderbook_delta":
                book.apply_delta(msg)
            elif msg_type == "ticker":
                yes_ask = msg.get("yes_ask_dollars") or msg.get("yes_ask")
                if yes_ask is not None:
                    price = float(yes_ask)
                    if price > 1.0:
                        price /= 100.0
                    self.price_data.setdefault(ticker, deque(maxlen=100)).append(price)
                volume = msg.get("volume_fp") or msg.get("volume")
                if volume is not None:
                    self.volumes[ticker] = int(float(volume))

            await self.evaluate_trade(ticker)
            return

        if msg_type == "error":
            logger.warning("WebSocket error message: %s", data)

    async def evaluate_trade(self, ticker: str) -> None:
        prices_deque = self.price_data.setdefault(ticker, deque(maxlen=100))
        mid = self._mid_price(ticker)
        if mid is not None:
            prices_deque.append(mid)

        prices = list(prices_deque)
        if len(prices) < self.config.hour_ws.min_price_history:
            return

        self.stats.evaluations += 1
        volume = self.volumes.get(ticker, 0)
        orderbook = self.orderbooks.get(ticker)
        crowd_book = orderbook.to_crowd_dict() if orderbook else None
        result = self.strategy.analyze(ticker, prices, volume, crowd_book)

        if result.signal == "HOLD":
            self.stats.holds += 1
            return

        if ticker in self.positions:
            if self.positions[ticker].side != result.signal:
                await self.exit_position(ticker, result)
            return

        await self.enter_position(ticker, result)

    async def enter_position(self, ticker: str, result: StrategyResult) -> None:
        ws_cfg = self.config.hour_ws
        if self.daily_pnl <= -ws_cfg.max_daily_loss:
            logger.warning("Daily loss limit reached; skipping entry")
            return
        if len(self.positions) >= ws_cfg.max_position_size:
            logger.warning("Max open positions reached; skipping entry")
            return

        price = result.price_cents / 100.0
        edge = abs(result.fair_value - price) * 100.0
        self._print_trade("ENTER", ticker, result, edge)

        if not self.orders_enabled:
            return

        side = "yes" if result.signal == "BUY" else "no"
        ok = await self._place_order(ticker, side, "buy", price)
        if ok:
            self.positions[ticker] = OpenPosition(
                side=result.signal,
                entry=price,
                entry_time=datetime.now(timezone.utc),
                confidence=result.confidence,
            )
            self.stats.trades += 1
            self._journal_trade(ticker, "enter", result)

    async def exit_position(self, ticker: str, result: StrategyResult) -> None:
        pos = self.positions.get(ticker)
        if pos is None:
            return
        price = result.price_cents / 100.0
        pnl_dollars = (
            (price - pos.entry)
            if pos.side == "BUY"
            else (pos.entry - price)
        )
        pnl_cents = pnl_dollars * 100.0
        self.daily_pnl += pnl_dollars
        console.print(f"\n[bold yellow]EXIT[/bold yellow] {ticker} | PnL: {pnl_cents:.2f}¢")
        if not self.orders_enabled:
            del self.positions[ticker]
            return

        exit_side = "no" if pos.side == "BUY" else "yes"
        ok = await self._place_order(ticker, exit_side, "buy", price)
        if ok:
            del self.positions[ticker]
            self.stats.trades += 1
            self._journal_trade(ticker, "exit", result, pnl=pnl_cents)

    async def _place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        price: float,
    ) -> bool:
        if self.dry_run:
            console.print(
                f"[cyan]DRY-RUN[/cyan] {action.upper()} {side.upper()} "
                f"{ticker} @ {price * 100:.2f}¢"
            )
            return True
        if not self.kalshi.authenticated:
            logger.error("Cannot place live order without Kalshi credentials")
            return False
        try:
            cents = max(1, min(99, int(round(price * 100))))
            kwargs: dict[str, Any] = {
                "ticker": ticker,
                "side": side,
                "action": action,
                "count": self.config.hour_ws.order_quantity,
            }
            if side == "yes":
                kwargs["yes_price"] = cents
            else:
                kwargs["no_price"] = cents
            self.kalshi.create_order(**kwargs)
            console.print(
                f"[green]ORDER[/green] {action.upper()} {side.upper()} "
                f"{ticker} @ {cents}¢"
            )
            return True
        except Exception as exc:
            logger.error("Order failed for %s: %s", ticker, exc)
            return False

    def _print_trade(self, label: str, ticker: str, result: StrategyResult, edge: float) -> None:
        console.print(f"\n[bold]{'ENTER' if label == 'ENTER' else 'EXIT'}[/bold] {ticker}")
        console.print(
            f"   Signal: {result.signal} | Conf: {result.confidence:.0f}% | Edge: {edge:.2f}¢"
        )
        console.print(
            f"   Price: {result.price_cents:.2f}¢ | Fair: {result.fair_value * 100:.2f}¢"
        )
        for signal in result.signals:
            console.print(f"   → {signal.reason}: +{signal.weight:.0f}")
        if result.crowd is not None:
            crowd = result.crowd
            console.print(
                f"   Crowd: {crowd.score:.0f}% "
                f"(Price:{crowd.price_score:.0f} Vol:{crowd.volume_score:.0f})"
            )

    def _journal_trade(
        self,
        ticker: str,
        action: str,
        result: StrategyResult,
        *,
        pnl: float | None = None,
    ) -> None:
        payload = {
            "horizon": "1h_ws",
            "action": action,
            "signal": result.signal,
            "price_cents": result.price_cents,
            "fair_value": result.fair_value,
            "pnl_cents": pnl,
            "crowd_score": result.crowd.score if result.crowd else None,
            "indicators": result.indicators,
        }
        self.journal.log_trade(
            strategy="hour_ws_crowd",
            ticker=ticker,
            side=result.signal.lower(),
            count=float(self.config.hour_ws.order_quantity),
            price=result.price_cents / 100.0,
            notional=(result.price_cents / 100.0) * self.config.hour_ws.order_quantity,
            edge=abs(result.fair_value - result.price_cents / 100.0),
            confidence=f"{result.confidence:.0f}",
            dry_run=self.dry_run,
            ok=True,
            detail=action,
            payload=payload,
        )

    async def run(self) -> None:
        ws_cfg = self.config.hour_ws
        mode = "DRY-RUN" if self.dry_run else "LIVE"
        console.print(
            f"[bold]Kalshi 1h WebSocket + Crowd Favorite[/bold] mode={mode}\n"
            f"Entry: {ws_cfg.min_entry_cents:.0f}-{ws_cfg.max_entry_cents:.0f}¢ | "
            f"Crowd: {ws_cfg.crowd_min_cents:.0f}-{ws_cfg.crowd_max_cents:.0f}¢"
        )
        if not self.kalshi.authenticated:
            if self.dry_run:
                console.print(
                    "[yellow]No Kalshi keys — using REST polling fallback (paper mode)[/yellow]"
                )
                await self.run_rest_fallback()
                return
            raise RuntimeError("Kalshi API credentials required for live WebSocket trading")

        tickers = self.refresh_markets()
        if not tickers:
            console.print("[yellow]No active 1-hour KXBTCD markets found[/yellow]")
            return
        console.print(f"Tracking {len(tickers)} hourly markets")

        self._running = True

        async def refresh_loop() -> None:
            while self._running:
                await asyncio.sleep(ws_cfg.market_refresh_sec)
                new_tickers = self.refresh_markets()
                if set(new_tickers) != set(self._market_tickers):
                    logger.info(
                        "Market set changed (%d → %d)",
                        len(self._market_tickers),
                        len(new_tickers),
                    )
                    self._market_tickers = new_tickers
                    await self.ws.close()
                    return

        refresh_task = asyncio.create_task(refresh_loop())
        try:
            while self._running:
                current = self.refresh_markets()
                if not current:
                    await asyncio.sleep(ws_cfg.reconnect_delay_sec)
                    continue
                await self.ws.run(
                    current,
                    self.handle_message,
                    reconnect_delay_sec=ws_cfg.reconnect_delay_sec,
                )
        finally:
            self._running = False
            refresh_task.cancel()
            await self.ws.close()
            self.close()

    async def run_rest_fallback(self) -> None:
        """Poll REST orderbooks when WebSocket auth is unavailable (paper mode)."""
        ws_cfg = self.config.hour_ws
        self._running = True
        try:
            while self._running:
                tickers = self.refresh_markets()
                for ticker in tickers[:20]:
                    try:
                        raw = self.kalshi.get_orderbook(ticker)
                        book = self.orderbooks.setdefault(ticker, LiveOrderbook())
                        book.load_snapshot(raw)
                        market = self.market_meta.get(ticker)
                        if market is not None:
                            mid = (market.yes_bid + market.yes_ask) / 2.0
                            if mid > 0:
                                self.price_data.setdefault(ticker, deque(maxlen=100)).append(mid)
                        await self.evaluate_trade(ticker)
                    except Exception as exc:
                        logger.debug("REST poll failed for %s: %s", ticker, exc)
                await asyncio.sleep(max(1.0, self.config.execution.poll_interval_sec))
        finally:
            self._running = False
            self.close()
