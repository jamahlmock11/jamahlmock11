"""Run alternative strategies alongside the forecast pipeline."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.config import AppConfig
from kalshi_bot.data.spot_hub import SpotPriceHub
from kalshi_bot.domain import MarketPosition, MarketSnapshot, OpenOrder
from kalshi_bot.market.poll_alignment import market_poll_snapshot
from kalshi_bot.strategies.alt_signal import AltTradeSignal
from kalshi_bot.strategies.longshot import extreme_poll_active
from kalshi_bot.strategies.mean_reversion import evaluate_mean_reversion
from kalshi_bot.strategies.orderbook_skew import evaluate_orderbook_skew
from kalshi_bot.strategies.spot_lag_arb import evaluate_spot_lag


@dataclass(frozen=True)
class AltStrategyResult:
    signals: tuple[AltTradeSignal, ...]
    notes: tuple[str, ...]


class AltStrategyRunner:
    def __init__(self, config: AppConfig, spot_hub: SpotPriceHub) -> None:
        self.config = config
        self.spot_hub = spot_hub

    def _filter_crowd_follow_signals(
        self,
        market: MarketSnapshot,
        *,
        seconds_remaining: float,
        signals: list[AltTradeSignal],
    ) -> list[AltTradeSignal]:
        ls = self.config.longshot
        if not ls.enabled or not ls.favorite_only:
            return signals
        poll = market_poll_snapshot(market.orderbook)
        if not extreme_poll_active(
            poll=poll,
            seconds_remaining=seconds_remaining,
            cfg=ls,
        ):
            return signals
        dominant = poll.dominant_side
        if dominant is None:
            return signals
        filtered: list[AltTradeSignal] = []
        for signal in signals:
            if signal.action != "buy":
                filtered.append(signal)
                continue
            if signal.side is dominant:
                filtered.append(signal)
        return filtered

    def evaluate(
        self,
        market: MarketSnapshot,
        *,
        seconds_remaining: float,
        position: MarketPosition | None = None,
        open_orders: tuple[OpenOrder, ...] = (),
        spot_price: float | None = None,
    ) -> AltStrategyResult:
        notes: list[str] = []
        signals: list[AltTradeSignal] = []

        spot_eval = evaluate_spot_lag(
            market,
            spot_hub=self.spot_hub,
            cfg=self.config.spot_lag,
            seconds_remaining=seconds_remaining,
        )
        notes.append(spot_eval.rationale)
        if spot_eval.signal is not None:
            signals.append(spot_eval.signal)

        skew = evaluate_orderbook_skew(
            market,
            cfg=self.config.orderbook_skew,
            seconds_remaining=seconds_remaining,
            spot_price=spot_price or (self.spot_hub.latest.price if self.spot_hub.latest else None),
        )
        if skew is not None:
            signals.append(skew)
            notes.append(skew.rationale)

        for signal in evaluate_mean_reversion(
            market,
            cfg=self.config.mean_reversion,
            open_orders=open_orders,
            position=position,
        ):
            signals.append(signal)
            notes.append(signal.rationale)

        signals = self._filter_crowd_follow_signals(
            market,
            seconds_remaining=seconds_remaining,
            signals=signals,
        )

        return AltStrategyResult(tuple(signals), tuple(notes))
