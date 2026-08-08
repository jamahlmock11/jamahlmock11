"""Monte Carlo finish simulation for terminal probability."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from kalshi_bot.domain import FeatureSnapshot


SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60


@dataclass(frozen=True)
class MonteCarloResult:
    p_up: float
    p_down: float
    paths_simulated: int
    mean_terminal_price: float
    std_terminal_price: float


def simulate_finish_probability(
    features: FeatureSnapshot,
    *,
    paths: int = 7500,
    orderbook_bias: float = 0.0,
    news_bias: float = 0.0,
    seed: int | None = None,
) -> MonteCarloResult:
    """
    Run Monte Carlo paths from current price using volatility, momentum, and biases.

    Returns UP/DOWN probabilities for finishing above strike.
    """
    if paths < 100:
        raise ValueError("paths must be at least 100")
    rng = random.Random(seed)

    spot = features.current_price
    strike = (
        features.settlement_effective_strike
        if features.settlement_effective_strike is not None
        else features.strike
    )
    seconds = max(features.seconds_remaining, 1.0)
    vol = max(features.realized_vol, 0.10)
    dt_year = seconds / SECONDS_PER_YEAR
    sigma = vol * math.sqrt(dt_year)

    drift = features.short_trend + features.acceleration * 30.0
    drift += orderbook_bias * 0.0002
    drift += news_bias * 0.0003

    above_count = 0
    terminals: list[float] = []

    for _ in range(paths):
        z = rng.gauss(0.0, 1.0)
        terminal = spot * math.exp((drift - 0.5 * vol * vol * dt_year) + sigma * z)
        terminals.append(terminal)
        if terminal >= strike:
            above_count += 1

    mean_terminal = sum(terminals) / len(terminals)
    variance = sum((t - mean_terminal) ** 2 for t in terminals) / len(terminals)
    std_terminal = math.sqrt(variance)

    p_up = above_count / paths
    return MonteCarloResult(
        p_up=p_up,
        p_down=1.0 - p_up,
        paths_simulated=paths,
        mean_terminal_price=mean_terminal,
        std_terminal_price=std_terminal,
    )
