"""Spot-lag arbitrage: trade when spot moves and Kalshi implied probability lags."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from kalshi_bot.config import SpotLagArbConfig
from kalshi_bot.data.spot_hub import SpotPriceHub
from kalshi_bot.domain import ContractSide, MarketSnapshot
from kalshi_bot.market.orderbook import estimate_buy_execution, microprice
from kalshi_bot.strategies.alt_signal import AltTradeSignal


@dataclass(frozen=True)
class SpotLagEvaluation:
    signal: AltTradeSignal | None
    rationale: str


def _fair_p_up(spot: float, strike: float, seconds_remaining: float) -> float:
    """Short-horizon fair UP probability from spot vs strike."""
    if strike <= 0 or spot <= 0:
        return 0.5
    # Scale move by remaining time (more time = less certain)
    horizon = max(seconds_remaining, 30.0)
    vol_per_sqrt_hour = 0.004
    sigma = vol_per_sqrt_hour * math.sqrt(horizon / 3600.0)
    if sigma <= 0:
        return 0.5
    z = (spot - strike) / (strike * sigma)
    return max(0.01, min(0.99, 0.5 + 0.5 * math.tanh(z)))


def evaluate_spot_lag(
    market: MarketSnapshot,
    *,
    spot_hub: SpotPriceHub,
    cfg: SpotLagArbConfig,
    seconds_remaining: float,
) -> SpotLagEvaluation:
    if not cfg.enabled:
        return SpotLagEvaluation(None, "spot lag arb disabled")

    move = spot_hub.move_since(cfg.lookback_seconds)
    tick = spot_hub.latest
    if move is None or tick is None:
        return SpotLagEvaluation(None, "spot feed unavailable")

    if abs(move) + 1e-12 < cfg.min_spot_move_usd:
        return SpotLagEvaluation(
            None,
            f"spot move ${abs(move):.0f} below ${cfg.min_spot_move_usd:.0f} threshold",
        )

    yes_mid = microprice(market.orderbook, ContractSide.YES)
    if yes_mid is None:
        return SpotLagEvaluation(None, "kalshi YES mid unavailable")

    fair_up = _fair_p_up(tick.price, market.strike, seconds_remaining)
    lag = fair_up - yes_mid
    if move < 0:
        lag = -lag

    if abs(lag) + 1e-12 < cfg.min_implied_lag:
        return SpotLagEvaluation(
            None,
            f"implied lag {lag:.1%} below {cfg.min_implied_lag:.1%}",
        )

    side = ContractSide.YES if lag > 0 else ContractSide.NO
    try:
        execution = estimate_buy_execution(market.orderbook, side, 1)
    except Exception as exc:
        return SpotLagEvaluation(None, f"no executable depth: {exc}")

    edge = (fair_up if side is ContractSide.YES else 1.0 - fair_up) - execution.executable_cost
    if edge + 1e-12 < cfg.min_edge:
        return SpotLagEvaluation(
            None,
            f"edge {edge:.1%} below {cfg.min_edge:.1%} after fees",
        )

    price = min(0.99, execution.average_price + 0.01)
    rationale = (
        f"spot moved ${move:+.0f} in {cfg.lookback_seconds:.0f}s; "
        f"fair={fair_up:.1%} kalshi={yes_mid:.1%} lag={lag:+.1%}"
    )
    return SpotLagEvaluation(
        AltTradeSignal(
            strategy="spot_lag",
            ticker=market.ticker,
            side=side,
            action="buy",
            quantity=1.0,
            limit_price=price,
            edge=edge,
            time_in_force="immediate_or_cancel",
            reason="spot-lag arb",
            intent_id=f"spotlag-{market.ticker}-{uuid.uuid4().hex[:8]}",
            rationale=rationale,
        ),
        rationale,
    )
