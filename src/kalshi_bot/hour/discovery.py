"""Discovery for Kalshi 1-hour BTC contracts (KXBTCD hourly window)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from kalshi_bot.config import HourStrategyConfig
from kalshi_bot.market.discovery import DiscoveryConfig, discover_current_market, DiscoveryResult


@dataclass(frozen=True)
class HourDiscoveryConfig:
    hour: HourStrategyConfig
    minimum_depth: float = 1.0
    maximum_spread: float = 0.14
    min_contract_duration: float = 2700.0
    max_contract_duration: float = 3900.0


def _contract_duration_seconds(raw: object, open_time: datetime | None, expiration: datetime | None) -> float | None:
    if open_time is None or expiration is None:
        return None
    return (expiration - open_time).total_seconds()


def _market_type(raw: object) -> str:
    if hasattr(raw, "market_type"):
        return str(getattr(raw, "market_type", "") or "").lower()
    if isinstance(raw, dict):
        return str(raw.get("market_type") or raw.get("type") or "").lower()
    return ""


def filter_hourly_markets(
    markets: list,
    *,
    config: HourDiscoveryConfig,
) -> list:
    series = config.hour.series_ticker.upper()
    filtered = []
    for raw in markets:
        market_type = _market_type(raw)
        if market_type and market_type not in {"1h", "hourly", "hour", "binary"}:
            continue

        open_time = getattr(raw, "open_time", None)
        close_time = getattr(raw, "close_time", None)
        if open_time is None and hasattr(raw, "open_time"):
            open_time = raw.open_time
        if close_time is None:
            close_time = getattr(raw, "close_time", None)

        duration = _contract_duration_seconds(raw, open_time, close_time)
        if duration is not None:
            if duration < config.min_contract_duration or duration > config.max_contract_duration:
                if market_type not in {"1h", "hourly", "hour"}:
                    continue

        ticker = str(getattr(raw, "ticker", "") or "")
        if not ticker.startswith(f"{series}-"):
            continue

        filtered.append(raw)
    return filtered


def select_nearest_strike_markets(
    markets: list,
    reference_price: float,
    *,
    count: int = 8,
) -> list:
    """Keep the strikes closest to spot so discovery checks tradable ATM books."""
    from kalshi_bot.market.discovery import _positive_strike

    scored: list[tuple[float, object]] = []
    for raw in markets:
        strike = _positive_strike(raw)
        if strike is None:
            continue
        scored.append((abs(strike - reference_price), raw))
    scored.sort(key=lambda item: (item[0], str(getattr(item[1], "ticker", ""))))
    return [raw for _, raw in scored[: max(count, 1)]]


def discover_hour_market(
    markets: list,
    *,
    orderbooks: dict | None = None,
    now: datetime,
    config: HourDiscoveryConfig,
    reference_price: float | None = None,
) -> DiscoveryResult:
    hour_cfg = config.hour
    discovery_cfg = DiscoveryConfig(
        series_ticker=hour_cfg.series_ticker,
        minimum_seconds_remaining=hour_cfg.min_seconds_remaining,
        maximum_seconds_remaining=hour_cfg.max_entry_seconds_remaining,
        minimum_depth=config.minimum_depth,
        maximum_spread=config.maximum_spread,
    )
    hourly = filter_hourly_markets(markets, config=config)
    if reference_price is not None and hourly:
        hourly = select_nearest_strike_markets(hourly, reference_price)
    return discover_current_market(
        hourly,
        orderbooks=orderbooks,
        now=now,
        config=discovery_cfg,
    )
