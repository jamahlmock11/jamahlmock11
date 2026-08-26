"""Technical indicators for the WebSocket 1-hour bot."""

from __future__ import annotations

import numpy as np


class Indicators:
    @staticmethod
    def rsi(prices: list[float], period: int = 14) -> float:
        if len(prices) < period + 1:
            return 50.0
        deltas = np.diff(prices[-period - 1 :])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = float(np.mean(gains)) if len(gains) > 0 else 0.001
        avg_loss = float(np.mean(losses)) if len(losses) > 0 else 0.001
        rs = avg_gain / avg_loss if avg_loss > 0 else 100.0
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def ma(prices: list[float], period: int) -> float:
        if len(prices) >= period:
            return float(np.mean(prices[-period:]))
        return float(prices[-1])

    @staticmethod
    def bollinger(prices: list[float], period: int = 20) -> tuple[float, float, float]:
        if len(prices) < period:
            return 1.0, 0.5, 0.0
        window = prices[-period:]
        mid = float(np.mean(window))
        std = float(np.std(window))
        return mid + 2 * std, mid, mid - 2 * std

    @staticmethod
    def momentum(prices: list[float], period: int = 10) -> float:
        if len(prices) < period + 1:
            return 0.0
        base = prices[-period - 1]
        if base == 0:
            return 0.0
        return (prices[-1] - base) / base * 100.0
