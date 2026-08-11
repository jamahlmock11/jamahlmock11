"""Mean-reversion maker strategy on panic YES prices."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from kalshi_bot.config import MeanReversionConfig
from kalshi_bot.domain import ContractSide, MarketPosition, MarketSnapshot, OpenOrder
from kalshi_bot.market.orderbook import microprice
from kalshi_bot.strategies.alt_signal import AltTradeSignal


@dataclass
class MeanReversionState:
    entry_prices: dict[str, float] = field(default_factory=dict)


def _maker_price(mid: float, offset: float, *, buy: bool) -> float:
    price = mid - offset if buy else mid + offset
    return max(0.01, min(0.99, price))


def evaluate_mean_reversion(
    market: MarketSnapshot,
    *,
    cfg: MeanReversionConfig,
    open_orders: tuple[OpenOrder, ...],
    position: MarketPosition | None,
) -> list[AltTradeSignal]:
    if not cfg.enabled:
        return []

    signals: list[AltTradeSignal] = []
    resting = [
        o for o in open_orders
        if o.status.lower() in {"open", "pending", "resting"}
    ]
    if len(resting) >= cfg.max_resting_orders:
        return signals

    yes_mid = microprice(market.orderbook, ContractSide.YES)
    no_mid = microprice(market.orderbook, ContractSide.NO)
    if yes_mid is None:
        return signals

    # Exit on 15¢ revert
    if position is not None and position.quantity > 0:
        entry = position.average_price
        exit_bid = (
            market.orderbook.yes_bid
            if position.side is ContractSide.YES
            else market.orderbook.no_bid
        )
        if exit_bid is not None and exit_bid - entry + 1e-12 >= cfg.revert_exit_cents:
            signals.append(
                AltTradeSignal(
                    strategy="mean_reversion",
                    ticker=market.ticker,
                    side=position.side,
                    action="sell",
                    quantity=position.quantity,
                    limit_price=max(0.01, exit_bid - 0.01),
                    edge=exit_bid - entry,
                    time_in_force="immediate_or_cancel",
                    reason=f"mean-reversion exit +{(exit_bid - entry) * 100:.0f}¢",
                    intent_id=f"mrev-exit-{market.ticker}-{uuid.uuid4().hex[:8]}",
                    rationale="revert target reached",
                )
            )
            return signals

    if yes_mid + 1e-12 <= cfg.cheap_threshold:
        price = _maker_price(yes_mid, cfg.maker_offset_cents, buy=True)
        signals.append(
            AltTradeSignal(
                strategy="mean_reversion",
                ticker=market.ticker,
                side=ContractSide.YES,
                action="buy",
                quantity=1.0,
                limit_price=price,
                edge=0.5 - price,
                time_in_force=cfg.time_in_force,
                reason=f"panic YES {yes_mid:.2f} ≤ {cfg.cheap_threshold:.2f}",
                intent_id=f"mrev-yes-{market.ticker}-{uuid.uuid4().hex[:8]}",
                rationale="resting maker bid below panic YES",
            )
        )
    elif yes_mid + 1e-12 >= cfg.rich_threshold and no_mid is not None:
        price = _maker_price(no_mid, cfg.maker_offset_cents, buy=True)
        signals.append(
            AltTradeSignal(
                strategy="mean_reversion",
                ticker=market.ticker,
                side=ContractSide.NO,
                action="buy",
                quantity=1.0,
                limit_price=price,
                edge=0.5 - price,
                time_in_force=cfg.time_in_force,
                reason=f"panic YES {yes_mid:.2f} ≥ {cfg.rich_threshold:.2f}",
                intent_id=f"mrev-no-{market.ticker}-{uuid.uuid4().hex[:8]}",
                rationale="resting maker bid on cheap NO vs rich YES",
            )
        )
    return signals
