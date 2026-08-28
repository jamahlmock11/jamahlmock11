"""Crowd-favorite + technical strategy for the WebSocket 1-hour bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kalshi_bot.config import HourWSConfig
from kalshi_bot.hour_ws.crowd import CrowdEngine, CrowdSnapshot
from kalshi_bot.hour_ws.indicators import Indicators


@dataclass(frozen=True)
class SignalDetail:
    action: str
    reason: str
    weight: float


@dataclass(frozen=True)
class StrategyResult:
    signal: str
    confidence: float
    fair_value: float
    price_cents: float
    signals: tuple[SignalDetail, ...] = ()
    crowd: CrowdSnapshot | None = None
    indicators: dict[str, float] = field(default_factory=dict)


class CrowdFavoriteStrategy:
    def __init__(self, cfg: HourWSConfig):
        self.cfg = cfg
        self.indicators = Indicators()
        self.crowd = CrowdEngine(cfg)

    def analyze(
        self,
        market_id: str,
        prices: list[float],
        volume: int,
        orderbook: dict[str, Any] | None,
    ) -> StrategyResult:
        if len(prices) < self.cfg.min_price_history:
            last = prices[-1] if prices else 0.5
            return StrategyResult(
                signal="HOLD",
                confidence=0.0,
                fair_value=last,
                price_cents=last * 100.0,
            )

        current = prices[-1]
        price_cents = current * 100.0

        rsi_val = self.indicators.rsi(prices)
        ma20 = self.indicators.ma(prices, 20)
        ma50 = self.indicators.ma(prices, 50)
        upper, _mid, lower = self.indicators.bollinger(prices)
        mom = self.indicators.momentum(prices)

        buy_score = 0.0
        sell_score = 0.0
        signals: list[SignalDetail] = []

        if rsi_val < 30:
            buy_score += 25
            signals.append(SignalDetail("BUY", "RSI oversold", 25))
        elif rsi_val > 70:
            sell_score += 25
            signals.append(SignalDetail("SELL", "RSI overbought", 25))

        if current > ma20 and ma20 > ma50:
            buy_score += 20
            signals.append(SignalDetail("BUY", "MA uptrend", 20))
        elif current < ma20 and ma20 < ma50:
            sell_score += 20
            signals.append(SignalDetail("SELL", "MA downtrend", 20))

        if current < lower + 0.03:
            buy_score += 25
            signals.append(SignalDetail("BUY", "Bollinger lower", 25))
        elif current > upper - 0.03:
            sell_score += 25
            signals.append(SignalDetail("SELL", "Bollinger upper", 25))

        if mom > 3:
            buy_score += 15
            signals.append(SignalDetail("BUY", f"Momentum +{mom:.1f}%", 15))
        elif mom < -3:
            sell_score += 15
            signals.append(SignalDetail("SELL", f"Momentum {mom:.1f}%", 15))

        crowd_data = self.crowd.update(market_id, current, volume, orderbook)
        crowd_bias = self.crowd.get_crowd_bias(market_id)
        crowd_conf = self.crowd.get_confidence(market_id)

        if self.cfg.crowd_min_cents <= price_cents <= self.cfg.crowd_max_cents:
            boost_factor = self.cfg.crowd_boost_factor
            if crowd_bias == "YES" and buy_score > sell_score:
                boost = crowd_conf * boost_factor
                buy_score += boost
                signals.append(SignalDetail("BUY", f"Crowd YES ({crowd_conf:.0f}%)", boost))
            elif crowd_bias == "NO" and sell_score > buy_score:
                boost = crowd_conf * boost_factor
                sell_score += boost
                signals.append(SignalDetail("SELL", f"Crowd NO ({crowd_conf:.0f}%)", boost))

        if not (self.cfg.min_entry_cents <= price_cents <= self.cfg.max_entry_cents):
            return StrategyResult(
                signal="HOLD",
                confidence=0.0,
                fair_value=current,
                price_cents=price_cents,
                signals=tuple(signals),
                crowd=crowd_data,
                indicators={"rsi": rsi_val, "ma20": ma20, "ma50": ma50, "momentum": mom},
            )

        min_confidence = self.cfg.min_confidence
        fair_move = self.cfg.fair_value_move_cents / 100.0
        min_edge = self.cfg.min_edge_cents / 100.0

        if buy_score > sell_score and buy_score > min_confidence:
            signal = "BUY"
            confidence = min(buy_score, 100.0)
            fair_value = current + fair_move
        elif sell_score > buy_score and sell_score > min_confidence:
            signal = "SELL"
            confidence = min(sell_score, 100.0)
            fair_value = current - fair_move
        else:
            return StrategyResult(
                signal="HOLD",
                confidence=0.0,
                fair_value=current,
                price_cents=price_cents,
                signals=tuple(signals),
                crowd=crowd_data,
                indicators={"rsi": rsi_val, "ma20": ma20, "ma50": ma50, "momentum": mom},
            )

        edge = abs(fair_value - current)
        if edge < min_edge:
            return StrategyResult(
                signal="HOLD",
                confidence=confidence,
                fair_value=fair_value,
                price_cents=price_cents,
                signals=tuple(signals),
                crowd=crowd_data,
                indicators={"rsi": rsi_val, "ma20": ma20, "ma50": ma50, "momentum": mom},
            )

        return StrategyResult(
            signal=signal,
            confidence=confidence,
            fair_value=fair_value,
            price_cents=price_cents,
            signals=tuple(signals),
            crowd=crowd_data,
            indicators={"rsi": rsi_val, "ma20": ma20, "ma50": ma50, "momentum": mom},
        )
