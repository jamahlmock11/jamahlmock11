"""Late-window orderbook skew microstructure signals."""

from __future__ import annotations

import math
import uuid

from kalshi_bot.config import OrderbookSkewConfig
from kalshi_bot.domain import ContractSide, MarketSnapshot
from kalshi_bot.market.orderbook import estimate_buy_execution, skew_top_n
from kalshi_bot.strategies.alt_signal import AltTradeSignal


def evaluate_orderbook_skew(
    market: MarketSnapshot,
    *,
    cfg: OrderbookSkewConfig,
    seconds_remaining: float,
    spot_price: float | None,
) -> AltTradeSignal | None:
    if not cfg.enabled:
        return None
    if seconds_remaining > cfg.max_seconds_remaining:
        return None

    skew = skew_top_n(market.orderbook, n=cfg.top_levels)
    if abs(skew) + 1e-12 < cfg.min_skew:
        return None

    if spot_price is not None and market.strike > 0:
        z = (spot_price - market.strike) / max(market.strike * 0.001, 1.0)
        if abs(z) + 1e-12 < cfg.min_z_distance:
            return None

    side = ContractSide.YES if skew > 0 else ContractSide.NO
    try:
        execution = estimate_buy_execution(market.orderbook, side, 1)
    except Exception:
        return None

    pseudo_prob = 0.5 + abs(skew) * 0.35
    edge = pseudo_prob - execution.executable_cost
    if edge + 1e-12 < cfg.min_edge:
        return None

    price = min(0.99, execution.average_price + 0.01)
    return AltTradeSignal(
        strategy="orderbook_skew",
        ticker=market.ticker,
        side=side,
        action="buy",
        quantity=1.0,
        limit_price=price,
        edge=edge,
        time_in_force="immediate_or_cancel",
        reason=f"late skew {skew:+.2f} with {seconds_remaining:.0f}s left",
        intent_id=f"skew-{market.ticker}-{uuid.uuid4().hex[:8]}",
        rationale=(
            f"top-{cfg.top_levels} skew={skew:+.2f}, "
            f"z={(spot_price - market.strike) / market.strike if spot_price else 0:.3f}"
        ),
    )
