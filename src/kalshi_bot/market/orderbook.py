"""Kalshi fixed-point order-book parsing and deterministic execution estimates."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from kalshi_bot.domain import (
    ContractSide,
    ExecutionEstimate,
    OrderBookSnapshot,
    OrderLevel,
    utc_datetime,
)


class OrderBookError(ValueError):
    pass


class InsufficientDepthError(OrderBookError):
    pass


def _price(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise OrderBookError(f"invalid level price {value!r}") from exc
    if 1 < result <= 100:
        result /= 100.0
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise OrderBookError(f"price must be within [0, 1], got {result!r}")
    return result


def _size(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise OrderBookError(f"invalid level size {value!r}") from exc
    if not math.isfinite(result) or result <= 0:
        raise OrderBookError(f"size must be positive and finite, got {result!r}")
    return result


def _parse_level(raw: Any) -> tuple[float, float]:
    if isinstance(raw, Mapping):
        price_value = raw.get("price")
        if price_value is None:
            price_value = raw.get("price_fp")
        size_value = raw.get("size")
        if size_value is None:
            size_value = raw.get("quantity")
        if size_value is None:
            size_value = raw.get("count")
        return _price(price_value), _size(size_value)
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and len(raw) >= 2:
        return _price(raw[0]), _size(raw[1])
    raise OrderBookError(f"malformed order-book level: {raw!r}")


def _levels(raw_levels: Any, *, reverse: bool) -> tuple[OrderLevel, ...]:
    if raw_levels in (None, []):
        return ()
    if not isinstance(raw_levels, Sequence) or isinstance(raw_levels, (str, bytes)):
        raise OrderBookError("order-book side must be an array")
    combined: dict[float, float] = {}
    for raw in raw_levels:
        price, size = _parse_level(raw)
        combined[price] = combined.get(price, 0.0) + size
    return tuple(
        OrderLevel(price=price, size=combined[price])
        for price in sorted(combined, reverse=reverse)
    )


def _complementary_asks(bids: tuple[OrderLevel, ...]) -> tuple[OrderLevel, ...]:
    return tuple(
        sorted(
            (OrderLevel(price=1.0 - level.price, size=level.size) for level in bids),
            key=lambda level: level.price,
        )
    )


def validate_orderbook(book: OrderBookSnapshot) -> None:
    for name in ("yes_bids", "yes_asks", "no_bids", "no_asks"):
        for level in getattr(book, name):
            if not 0 <= level.price <= 1 or level.size <= 0:
                raise OrderBookError(f"{name} contains an invalid level")
    if book.yes_bid is not None and book.yes_ask is not None and book.yes_bid >= book.yes_ask:
        raise OrderBookError("YES book is crossed or locked")
    if book.no_bid is not None and book.no_ask is not None and book.no_bid >= book.no_ask:
        raise OrderBookError("NO book is crossed or locked")
    if book.yes_bid is not None and book.no_bid is not None and book.yes_bid + book.no_bid >= 1:
        raise OrderBookError("complementary YES/NO bids cross or lock")


def parse_orderbook_fp(
    payload: Mapping[str, Any],
    *,
    timestamp: datetime | None = None,
) -> OrderBookSnapshot:
    """Parse bid-only `orderbook_fp`; opposite bids define executable asks."""
    raw: Any = payload.get("orderbook_fp", payload)
    if isinstance(raw, Mapping) and "orderbook" in raw and isinstance(raw["orderbook"], Mapping):
        raw = raw["orderbook"]
    if not isinstance(raw, Mapping):
        raise OrderBookError("orderbook_fp payload must be an object")
    yes_bids = _levels(
        raw.get("yes_dollars")
        if raw.get("yes_dollars") is not None
        else raw.get("yes"),
        reverse=True,
    )
    no_bids = _levels(
        raw.get("no_dollars")
        if raw.get("no_dollars") is not None
        else raw.get("no"),
        reverse=True,
    )
    if not yes_bids and not no_bids:
        raise OrderBookError("orderbook has no bid levels")
    book = OrderBookSnapshot(
        timestamp=utc_datetime(timestamp or datetime.now(timezone.utc)),
        yes_bids=yes_bids,
        yes_asks=_complementary_asks(no_bids),
        no_bids=no_bids,
        no_asks=_complementary_asks(yes_bids),
    )
    validate_orderbook(book)
    return book


def depth(
    book: OrderBookSnapshot,
    side: ContractSide,
    *,
    asks: bool = True,
    max_price: float | None = None,
) -> float:
    levels = book.levels(side, asks=asks)
    return sum(
        level.size
        for level in levels
        if max_price is None or level.price <= max_price
    )


def notional_depth(book: OrderBookSnapshot, side: ContractSide, *, asks: bool = True) -> float:
    return sum(level.price * level.size for level in book.levels(side, asks=asks))


def spread(book: OrderBookSnapshot, side: ContractSide) -> float | None:
    return book.yes_spread if side is ContractSide.YES else book.no_spread


def imbalance(book: OrderBookSnapshot) -> float:
    """Contract-depth imbalance: positive means more YES than NO bid support."""
    yes = sum(level.size for level in book.yes_bids)
    no = sum(level.size for level in book.no_bids)
    return (yes - no) / (yes + no) if yes + no else 0.0


def estimate_buy_execution(
    book: OrderBookSnapshot,
    side: ContractSide,
    quantity: float,
    *,
    fee_rate: float = 0.0,
    fee_per_contract: float = 0.0,
    slippage_bps: float = 0.0,
    slippage_per_contract: float = 0.0,
    require_full_fill: bool = True,
) -> ExecutionEstimate:
    """Walk asks, then add explicit fees and adverse slippage per contract."""
    if not math.isfinite(quantity) or quantity <= 0:
        raise ValueError("quantity must be positive and finite")
    if min(fee_rate, fee_per_contract, slippage_bps, slippage_per_contract) < 0:
        raise ValueError("fees and slippage cannot be negative")
    remaining = quantity
    raw_cost = 0.0
    filled = 0.0
    consumed = 0
    for level in book.levels(side, asks=True):
        take = min(remaining, level.size)
        if take <= 0:
            continue
        raw_cost += take * level.price
        filled += take
        remaining -= take
        consumed += 1
        if remaining <= 1e-12:
            break
    if filled <= 0 or (require_full_fill and remaining > 1e-12):
        raise InsufficientDepthError(
            f"{side.value} asks fill {filled:g} of requested {quantity:g}"
        )
    average = raw_cost / filled
    # Kalshi-style probability-contract fee shape can be represented by fee_rate.
    variable_fee = fee_rate * average * (1.0 - average)
    fee_each = fee_per_contract + variable_fee
    slip_each = slippage_per_contract + average * slippage_bps / 10_000.0
    executable_each = average + fee_each + slip_each
    total = executable_each * filled
    return ExecutionEstimate(
        side=side,
        quantity=quantity,
        filled_quantity=filled,
        average_price=average,
        fee_per_contract=fee_each,
        slippage_per_contract=slip_each,
        total_cost=total,
        executable_cost=executable_each,
        levels_consumed=consumed,
    )


estimate_buy_execution_price = estimate_buy_execution
