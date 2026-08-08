"""Discovery and strict validation for the currently tradable KXBTC15M contract."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kalshi_bot.domain import MarketSnapshot, OrderBookSnapshot, utc_datetime
from kalshi_bot.market.orderbook import OrderBookError, parse_orderbook_fp


@dataclass(frozen=True)
class DiscoveryConfig:
    series_ticker: str = "KXBTC15M"
    minimum_seconds_remaining: float = 30.0
    maximum_seconds_remaining: float = 15 * 60.0
    minimum_depth: float = 1.0
    maximum_spread: float = 0.15


@dataclass(frozen=True)
class MarketValidation:
    accepted: bool
    snapshot: MarketSnapshot | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryResult:
    market: MarketSnapshot | None
    rejections: Mapping[str, tuple[str, ...]]


def _get(raw: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = raw.get(key) if isinstance(raw, Mapping) else getattr(raw, key, None)
        if value is not None and value != "":
            return value
    return default


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return utc_datetime(value)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if value:
        try:
            return utc_datetime(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _positive_strike(raw: Any) -> float | None:
    for key in (
        "strike",
        "floor_strike",
        "target_price",
        "target",
        "strike_value",
    ):
        value = _get(raw, key)
        if value is None:
            continue
        try:
            strike = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(strike) and strike > 0:
            return strike
    # Only explicitly labelled strike/floor/target text is valid; never use spot.
    text = " ".join(
        str(_get(raw, key, default=""))
        for key in ("rules_primary", "rules", "title", "subtitle")
    )
    match = re.search(
        r"(?:strike|floor|target(?:\s+price)?)\s*(?:is|:|=)?\s*\$?\s*"
        r"([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)",
        text,
        flags=re.IGNORECASE,
    )
    return float(match.group(1).replace(",", "")) if match else None


def _valid_reference(text: str) -> bool:
    normalized = " ".join(text.lower().replace("-", " ").split())
    return (
        "brti" in normalized
        or "cme cf bitcoin real time index" in normalized
        or ("cf benchmarks" in normalized and "bitcoin real time index" in normalized)
    )


def validate_market(
    raw_market: Any,
    *,
    orderbook: OrderBookSnapshot | Mapping[str, Any] | None = None,
    now: datetime,
    config: DiscoveryConfig | None = None,
) -> MarketValidation:
    """Validate every settlement and execution invariant, returning all failures."""
    config = config or DiscoveryConfig()
    now = utc_datetime(now)
    reasons: list[str] = []

    if isinstance(raw_market, MarketSnapshot):
        snapshot = raw_market
        ticker = snapshot.ticker
        status = snapshot.status
        rules = snapshot.rules
        strike = snapshot.strike
        expiration = snapshot.expiration
        open_time = snapshot.open_time
        reference = snapshot.reference
        book = snapshot.orderbook
    else:
        ticker = str(_get(raw_market, "ticker", default=""))
        status = str(_get(raw_market, "status", default="")).lower()
        rules = str(_get(raw_market, "rules_primary", "rules", default=""))
        reference = str(
            _get(raw_market, "settlement_source", "reference", default=rules)
        )
        strike = _positive_strike(raw_market)
        expiration = _datetime(_get(raw_market, "close_time", "expiration", "expiration_time"))
        open_time = _datetime(_get(raw_market, "open_time", "start_time"))
        raw_book = orderbook if orderbook is not None else _get(raw_market, "orderbook_fp", "orderbook")
        try:
            book = (
                raw_book
                if isinstance(raw_book, OrderBookSnapshot)
                else parse_orderbook_fp(
                    {"orderbook_fp": raw_book} if raw_book is not None else {},
                    timestamp=now,
                )
            )
        except (OrderBookError, TypeError) as exc:
            reasons.append(f"invalid orderbook: {exc}")
            book = None

    if not ticker.startswith(f"{config.series_ticker}-"):
        reasons.append(f"ticker is not a {config.series_ticker} contract")
    if str(status).lower() not in {"open", "active"}:
        reasons.append("market status is not active/open")
    if open_time is None:
        reasons.append("open time is missing or malformed")
    elif open_time > now:
        reasons.append("market has not opened")
    if expiration is None:
        reasons.append("expiration is missing or malformed")
        seconds_remaining = None
    else:
        seconds_remaining = (expiration - now).total_seconds()
        if seconds_remaining <= 0:
            reasons.append("market is expired")
        elif seconds_remaining < config.minimum_seconds_remaining:
            reasons.append("too little time remains")
        elif seconds_remaining > config.maximum_seconds_remaining:
            reasons.append("too much time remains")
    if strike is None or not math.isfinite(strike) or strike <= 0:
        reasons.append("explicit positive strike/floor/target is required")
    settlement_text = f"{rules} {reference}"
    if not _valid_reference(settlement_text):
        reasons.append("settlement rules do not explicitly reference CME CF BRTI")

    if book is not None:
        if book.yes_ask is None or book.no_ask is None:
            reasons.append("both YES and NO executable asks are required")
        if book.yes_bid is None or book.no_bid is None:
            reasons.append("both YES and NO executable bids are required")
        if book.yes_spread is None or book.no_spread is None:
            reasons.append("two-sided YES and NO spreads are required")
        elif max(book.yes_spread, book.no_spread) > config.maximum_spread:
            reasons.append("orderbook spread exceeds maximum")
        yes_depth = sum(level.size for level in book.yes_asks)
        no_depth = sum(level.size for level in book.no_asks)
        if min(yes_depth, no_depth) < config.minimum_depth:
            reasons.append("executable liquidity is below minimum")

    snapshot: MarketSnapshot | None = None
    if (
        book is not None
        and expiration is not None
        and open_time is not None
        and strike is not None
        and math.isfinite(strike)
        and strike > 0
    ):
        current_position = _get(raw_market, "current_position") if not isinstance(raw_market, MarketSnapshot) else raw_market.current_position
        open_orders = _get(raw_market, "open_orders", default=()) if not isinstance(raw_market, MarketSnapshot) else raw_market.open_orders
        snapshot = MarketSnapshot(
            ticker=ticker,
            status=str(status),
            rules=rules,
            strike=strike,
            expiration=expiration,
            open_time=open_time,
            reference=reference,
            orderbook=book,
            current_position=current_position,
            open_orders=tuple(open_orders or ()),
            valid=not reasons,
            rejection_reasons=tuple(reasons),
        )
    return MarketValidation(accepted=not reasons, snapshot=snapshot, reasons=tuple(reasons))


def discover_current_market(
    markets: Sequence[Any],
    *,
    orderbooks: Mapping[str, OrderBookSnapshot | Mapping[str, Any]] | None = None,
    now: datetime,
    config: DiscoveryConfig | None = None,
) -> DiscoveryResult:
    """Select the valid open interval with the nearest expiration."""
    config = config or DiscoveryConfig()
    accepted: list[MarketSnapshot] = []
    rejections: dict[str, tuple[str, ...]] = {}
    for raw in markets:
        ticker = str(_get(raw, "ticker", default="<missing-ticker>"))
        book = orderbooks.get(ticker) if orderbooks is not None else None
        validation = validate_market(raw, orderbook=book, now=now, config=config)
        if validation.accepted and validation.snapshot is not None:
            accepted.append(validation.snapshot)
        else:
            rejections[ticker] = validation.reasons
    selected = min(accepted, key=lambda market: (market.expiration, market.ticker)) if accepted else None
    return DiscoveryResult(market=selected, rejections=rejections)


class MarketDiscovery:
    def __init__(
        self,
        config: DiscoveryConfig | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config or DiscoveryConfig()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def select(
        self,
        markets: Sequence[Any],
        *,
        orderbooks: Mapping[str, OrderBookSnapshot | Mapping[str, Any]] | None = None,
        now: datetime | None = None,
    ) -> DiscoveryResult:
        return discover_current_market(
            markets,
            orderbooks=orderbooks,
            now=now or self.clock(),
            config=self.config,
        )
