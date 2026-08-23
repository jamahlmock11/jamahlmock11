"""Async 15-minute Kalshi orchestrator with 3-phase trading architecture."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from brti_engine import BRTIEngine, BRTIWebSocketManager
from config import BotSettings, HARD_STOP_SEC, PHASE1_END_SEC, PHASE2_END_SEC, load_settings, resolve_private_key_path
from kalshi_client import ActiveContract, AsyncKalshiClient
from kalshi_ws import KalshiWebSocketSubscriber
from orderbook_live import LiveOrderBook, TopOfBook, handle_orderbook_update

logger = logging.getLogger(__name__)


class Phase(IntEnum):
    IDLE = 0
    PHASE1 = 1
    PHASE2 = 2
    PHASE3 = 3
    STOPPED = 4


@dataclass
class CycleState:
    contract: ActiveContract
    brti: BRTIEngine
    live_book: LiveOrderBook
    top_of_book: TopOfBook | None = None
    last_settlement_second: int = -1
    resting_order_ids: list[str] | None = None

    def __post_init__(self) -> None:
        if self.resting_order_ids is None:
            self.resting_order_ids = []


def _quote_book(state: CycleState) -> TopOfBook:
    """Prefer live WebSocket top-of-book; fall back to REST snapshot."""
    if state.top_of_book and state.top_of_book.updated:
        return state.top_of_book
    contract = state.contract
    return TopOfBook(
        ticker=contract.ticker,
        yes_bid=contract.yes_bid,
        yes_ask=contract.yes_ask,
        no_bid=contract.no_bid,
        no_ask=contract.no_ask,
        yes_bid_size=contract.yes_bid_size,
        yes_ask_size=contract.yes_ask_size,
        updated=False,
    )


def determine_phase(elapsed_sec: float, *, hard_stop_sec: int = HARD_STOP_SEC) -> Phase:
    if elapsed_sec >= hard_stop_sec:
        return Phase.STOPPED
    if elapsed_sec < PHASE1_END_SEC:
        return Phase.PHASE1
    if elapsed_sec < PHASE2_END_SEC:
        return Phase.PHASE2
    return Phase.PHASE3


def phase1_signal(
    spot: float,
    strike: float,
    drift_pct: float,
) -> str | None:
    if strike <= 0 or spot <= 0:
        return None
    drift = abs(spot - strike) / strike
    if drift < drift_pct:
        return None
    return "UP" if spot > strike else "DOWN"


def phase3_fair_yes_cents(metrics: dict[str, Any], spot: float, strike: float) -> int:
    """Map settlement math to a rough fair YES price in cents."""
    if metrics.get("mathematical_certainty"):
        return 99 if spot >= strike else 1
    distance = metrics.get("price_distance", 0.0)
    # $100 of cushion ≈ 10¢ probability shift in the final minute
    shift = max(-40, min(40, int(distance / 10.0)))
    base = 50 + shift
    return max(1, min(99, base))


class FifteenMinuteOrchestrator:
    def __init__(self, settings: BotSettings) -> None:
        self.settings = settings
        self.ws = BRTIWebSocketManager(
            vwap_interval_ms=settings.vwap_interval_ms,
            vwap_lookback_sec=settings.vwap_lookback_sec,
        )
        key_path = str(resolve_private_key_path(settings.kalshi_private_key_path))
        self.kalshi = AsyncKalshiClient(
            settings.kalshi_url,
            api_key_id=settings.kalshi_api_key_id,
            private_key_path=key_path,
        )
        self._cycle: CycleState | None = None

    async def run(self) -> None:
        await self.ws.start()
        try:
            while True:
                contract = await self.kalshi.fetch_active_contract(self.settings.series_ticker)
                if contract is None:
                    logger.info("No active %s contract; waiting 5s", self.settings.series_ticker)
                    await asyncio.sleep(5.0)
                    continue
                await self._run_contract_cycle(contract)
        finally:
            await self.ws.stop()
            await self.kalshi.close()

    async def _run_contract_cycle(self, contract: ActiveContract) -> None:
        logger.info(
            "Starting cycle %s strike=%.2f open=%s close=%s phases=[1:%s 2:%s 3:%s]",
            contract.ticker,
            contract.strike,
            contract.open_time.isoformat(),
            contract.close_time.isoformat(),
            self.settings.phase1_enabled,
            self.settings.phase2_enabled,
            self.settings.phase3_enabled,
        )
        brti = BRTIEngine(strike_price=contract.strike)
        live_book = LiveOrderBook(ticker=contract.ticker)
        state = CycleState(contract=contract, brti=brti, live_book=live_book)
        self._cycle = state

        kalshi_ws = KalshiWebSocketSubscriber(
            self.kalshi.signer,
            is_demo=self.settings.kalshi_env.lower() == "demo",
        )

        async def on_orderbook_message(payload: dict[str, Any]) -> None:
            top = await handle_orderbook_update(payload, live_book)
            if isinstance(top, TopOfBook):
                state.top_of_book = top

        ws_task = asyncio.create_task(
            kalshi_ws.stream_order_book(contract.ticker, on_orderbook_message),
            name=f"kalshi-ws-{contract.ticker}",
        )
        settlement_task = asyncio.create_task(self._settlement_ticker(state))
        try:
            async for vwap in self.ws.vwap_stream():
                if vwap > 0:
                    state.brti.update_spot(vwap)
                elapsed = contract.seconds_elapsed
                phase = determine_phase(elapsed, hard_stop_sec=self.settings.phases.hard_stop)

                if phase is Phase.STOPPED:
                    await self._hard_stop(state)
                    break
                if elapsed >= self.settings.phases.contract_end:
                    break

                if phase is Phase.PHASE1 and self.settings.phase1_enabled:
                    await self._phase1(state, vwap)
                elif phase is Phase.PHASE2 and self.settings.phase2_enabled:
                    await self._phase2(state)
                elif phase is Phase.PHASE3 and self.settings.phase3_enabled:
                    await self._phase3(state, vwap)

                if contract.seconds_remaining <= 0:
                    break
        finally:
            kalshi_ws.stop()
            ws_task.cancel()
            settlement_task.cancel()
            await asyncio.gather(ws_task, settlement_task, return_exceptions=True)
            await self._hard_stop(state)
            self._cycle = None

    async def _settlement_ticker(self, state: CycleState) -> None:
        """Record one BRTI settlement tick per second during minute 14–15."""
        while True:
            elapsed = state.contract.seconds_elapsed
            if elapsed < PHASE2_END_SEC:
                await asyncio.sleep(0.2)
                continue
            if elapsed >= self.settings.phases.hard_stop:
                return
            second_index = int(elapsed - PHASE2_END_SEC)
            if 0 <= second_index < 60 and second_index != state.last_settlement_second:
                state.brti.record_settlement_tick(second_index)
                state.last_settlement_second = second_index
            await asyncio.sleep(0.2)

    async def _phase1(self, state: CycleState, spot: float) -> None:
        signal = phase1_signal(spot, state.contract.strike, self.settings.phase1_drift_pct)
        if signal:
            logger.debug(
                "Phase1 drift signal=%s spot=%.2f strike=%.2f drift=%.3f%%",
                signal,
                spot,
                state.contract.strike,
                abs(spot - state.contract.strike) / state.contract.strike * 100,
            )

    async def _phase2(self, state: CycleState) -> None:
        book = _quote_book(state)
        spread_cents = book.spread_cents
        if spread_cents > self.settings.risk.max_spread_cents:
            return
        if state.resting_order_ids:
            return

        offset = self.settings.phase2_maker_offset_cents
        bid_cents = max(1, min(99, int(round(book.yes_bid * 100)) + offset))
        ask_cents = max(1, min(99, int(round(book.yes_ask * 100)) - offset))

        size_bid = AsyncKalshiClient.safe_order_size(
            limit_price_cents=bid_cents,
            depth_at_price=book.yes_bid_size,
            spread_cents=spread_cents,
            max_contracts=self.settings.risk.max_contracts_per_order,
            max_spread_cents=self.settings.risk.max_spread_cents,
            min_book_depth=self.settings.risk.min_book_depth_contracts,
            max_price_sweep_cents=self.settings.risk.max_price_sweep_cents,
        )
        if size_bid <= 0 or self.settings.dry_run:
            return

        logger.info(
            "Phase2 passive bid %s YES %dct x%d (spread=%dct, live=%s)",
            state.contract.ticker,
            bid_cents,
            size_bid,
            spread_cents,
            book.updated,
        )
        if not self.kalshi.authenticated:
            return
        try:
            resp = await self.kalshi.create_order(
                state.contract.ticker,
                side="yes",
                action="buy",
                count=size_bid,
                yes_price=bid_cents,
                time_in_force="good_til_canceled",
                client_order_id=f"phase2-bid-{state.contract.ticker}",
            )
            order_id = (resp.get("order") or {}).get("order_id") or resp.get("order_id")
            if order_id:
                state.resting_order_ids.append(str(order_id))
        except Exception as exc:
            logger.warning("Phase2 order failed: %s", exc)

        # Optional passive ask — sell YES at improved ask (maker rebate capture)
        size_ask = AsyncKalshiClient.safe_order_size(
            limit_price_cents=ask_cents,
            depth_at_price=book.yes_ask_size,
            spread_cents=spread_cents,
            max_contracts=self.settings.risk.max_contracts_per_order,
            max_spread_cents=self.settings.risk.max_spread_cents,
            min_book_depth=self.settings.risk.min_book_depth_contracts,
            max_price_sweep_cents=self.settings.risk.max_price_sweep_cents,
        )
        if size_ask > 0 and self.kalshi.authenticated and not self.settings.dry_run:
            try:
                resp = await self.kalshi.create_order(
                    state.contract.ticker,
                    side="yes",
                    action="sell",
                    count=size_ask,
                    yes_price=ask_cents,
                    time_in_force="good_til_canceled",
                    client_order_id=f"phase2-ask-{state.contract.ticker}",
                )
                order_id = (resp.get("order") or {}).get("order_id") or resp.get("order_id")
                if order_id:
                    state.resting_order_ids.append(str(order_id))
            except Exception as exc:
                logger.warning("Phase2 ask failed: %s", exc)

    async def _phase3(self, state: CycleState, spot: float) -> None:
        second_index = max(0, int(state.contract.seconds_elapsed - PHASE2_END_SEC))
        metrics = state.brti.calculate_probability_metrics(
            second_index,
            certainty_distance_usd=self.settings.phase3_certainty_distance_usd,
            certainty_max_remaining=self.settings.phase3_certainty_max_remaining,
        )
        fair_yes = phase3_fair_yes_cents(metrics, spot, state.contract.strike)
        book = _quote_book(state)
        market_yes = int(round(book.yes_ask * 100))
        mispricing = fair_yes - market_yes
        if abs(mispricing) < self.settings.phase3_min_mispricing_cents:
            return
        if book.spread_cents > self.settings.risk.max_spread_cents:
            return

        if mispricing > 0:
            side, action, price_cents = "yes", "buy", market_yes
            depth = book.yes_ask_size
        else:
            side, action, price_cents = "no", "buy", int(round(book.no_ask * 100))
            depth = book.no_ask_size

        size = AsyncKalshiClient.safe_order_size(
            limit_price_cents=price_cents,
            depth_at_price=depth,
            spread_cents=book.spread_cents,
            max_contracts=self.settings.risk.max_contracts_per_order,
            max_spread_cents=self.settings.risk.max_spread_cents,
            min_book_depth=self.settings.risk.min_book_depth_contracts,
            max_price_sweep_cents=self.settings.risk.max_price_sweep_cents,
        )
        if size <= 0:
            return

        logger.info(
            "Phase3 IOC %s %s %s %dct x%d fair=%dct mispricing=%+dct live=%s metrics=%s",
            state.contract.ticker,
            action.upper(),
            side.upper(),
            price_cents,
            size,
            fair_yes,
            mispricing,
            book.updated,
            metrics,
        )
        if self.settings.dry_run or not self.kalshi.authenticated:
            return

        try:
            if side == "yes":
                await self.kalshi.create_order(
                    state.contract.ticker,
                    side="yes",
                    action=action,
                    count=size,
                    yes_price=price_cents,
                    time_in_force="immediate_or_cancel",
                    client_order_id=f"phase3-{state.contract.ticker}-{second_index}",
                )
            else:
                await self.kalshi.create_order(
                    state.contract.ticker,
                    side="no",
                    action=action,
                    count=size,
                    no_price=price_cents,
                    time_in_force="immediate_or_cancel",
                    client_order_id=f"phase3-{state.contract.ticker}-{second_index}",
                )
        except Exception as exc:
            logger.warning("Phase3 IOC failed: %s", exc)

    async def _hard_stop(self, state: CycleState) -> None:
        if self.settings.dry_run or not self.kalshi.authenticated:
            state.resting_order_ids.clear()
            return
        cancelled = await self.kalshi.cancel_all_orders(ticker=state.contract.ticker)
        state.resting_order_ids.clear()
        if cancelled:
            logger.info("Hard stop at 14:55 — cancelled %d resting orders on %s", cancelled, state.contract.ticker)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    orchestrator = FifteenMinuteOrchestrator(settings)
    await orchestrator.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down")
