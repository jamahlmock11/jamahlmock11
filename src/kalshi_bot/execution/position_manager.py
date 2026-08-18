"""In-memory, venue-neutral lifecycle management for binary positions."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Mapping

from kalshi_bot.domain import ContractSide, utc_datetime


class PositionManagerError(ValueError):
    """Base class for rejected position intents."""


class DuplicateIntentError(PositionManagerError):
    """Raised when an intent identifier has already been submitted."""


class PositionLimitError(PositionManagerError):
    """Raised when a contract lifecycle limit would be exceeded."""


class PositionConflictError(PositionManagerError):
    """Raised for pyramiding or an entry before an opposite-side exit."""


@dataclass(frozen=True)
class PositionManagerConfig:
    """Explicit lifecycle limits; sizing remains the caller's responsibility."""

    max_flips_per_contract: int = 2
    max_trades_per_contract: int = 4
    pyramiding_enabled: bool = False

    def __post_init__(self) -> None:
        if self.max_flips_per_contract < 0:
            raise ValueError("max_flips_per_contract cannot be negative")
        if self.max_trades_per_contract <= 0:
            raise ValueError("max_trades_per_contract must be positive")


@dataclass(frozen=True)
class EntryRecord:
    """Accepted opening fill."""

    intent_id: str
    contract: str
    side: ContractSide
    quantity: float
    price: float
    fee: float
    timestamp: datetime


@dataclass(frozen=True)
class ExitRecord:
    """Accepted closing fill or final settlement."""

    intent_id: str
    contract: str
    side: ContractSide
    quantity: float
    entry_price: float
    exit_price: float
    entry_fee: float
    exit_fee: float
    realized_pnl: float
    timestamp: datetime
    reason: str


@dataclass(frozen=True)
class RejectedIntent:
    """Auditable rejected lifecycle request."""

    intent_id: str
    contract: str
    operation: str
    reason: str
    timestamp: datetime


@dataclass(frozen=True)
class Position:
    """One non-pyramided open binary position."""

    contract: str
    side: ContractSide
    quantity: float
    entry_price: float
    entry_fee: float
    opened_at: datetime
    entry_intent_id: str


class PositionManager:
    """Track paper or externally executed fills without venue side effects.

    The manager does not place orders or choose quantities. A side change must
    be represented by one exit intent followed by a distinct entry intent.
    """

    def __init__(
        self,
        config: PositionManagerConfig | None = None,
        *,
        mode: str = "paper",
    ) -> None:
        if not mode:
            raise ValueError("mode must be non-empty")
        self.config = config or PositionManagerConfig()
        self.mode = mode
        self._positions: dict[str, Position] = {}
        self._entries: list[EntryRecord] = []
        self._exits: list[ExitRecord] = []
        self._rejections: list[RejectedIntent] = []
        self._intent_ids: set[str] = set()
        self._trade_counts: dict[str, int] = {}
        self._flip_counts: dict[str, int] = {}
        self._last_sides: dict[str, ContractSide] = {}
        self._realized_pnl = 0.0

    @property
    def positions(self) -> Mapping[str, Position]:
        """Return a read-only snapshot of open positions."""
        return dict(self._positions)

    @property
    def entries(self) -> tuple[EntryRecord, ...]:
        """Return accepted entries in submission order."""
        return tuple(self._entries)

    @property
    def exits(self) -> tuple[ExitRecord, ...]:
        """Return accepted exits and settlements in submission order."""
        return tuple(self._exits)

    @property
    def rejections(self) -> tuple[RejectedIntent, ...]:
        """Return rejected intents in submission order."""
        return tuple(self._rejections)

    @property
    def realized_pnl(self) -> float:
        """Return cumulative realized P&L after all explicit fees."""
        return self._realized_pnl

    def position(self, contract: str) -> Position | None:
        """Return the open position for a contract, if any."""
        return self._positions.get(contract)

    def seed_position(
        self,
        *,
        contract: str,
        side: ContractSide,
        quantity: float,
        entry_price: float,
        timestamp: datetime,
        fee: float = 0.0,
    ) -> None:
        """Hydrate an externally verified position without creating a new trade."""
        self._validate_fill(quantity, entry_price, fee)
        if contract in self._positions:
            raise PositionConflictError("position is already seeded")
        observed_at = utc_datetime(timestamp)
        intent_id = f"seed-{contract}"
        self._positions[contract] = Position(
            contract=contract,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            entry_fee=fee,
            opened_at=observed_at,
            entry_intent_id=intent_id,
        )
        self._intent_ids.add(intent_id)
        self._last_sides[contract] = side

    @staticmethod
    def _validate_fill(quantity: float, price: float, fee: float) -> None:
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("quantity must be positive and finite")
        if not math.isfinite(price) or not 0.0 <= price <= 1.0:
            raise ValueError("price must be finite and within [0, 1]")
        if not math.isfinite(fee) or fee < 0:
            raise ValueError("fee must be non-negative and finite")

    def _begin_intent(
        self,
        intent_id: str,
        contract: str,
        operation: str,
        timestamp: datetime,
    ) -> datetime:
        observed_at = utc_datetime(timestamp)
        if not intent_id:
            raise ValueError("intent_id must be non-empty")
        if not contract:
            raise ValueError("contract must be non-empty")
        if intent_id in self._intent_ids:
            rejection = RejectedIntent(
                intent_id=intent_id,
                contract=contract,
                operation=operation,
                reason="duplicate intent_id",
                timestamp=observed_at,
            )
            self._rejections.append(rejection)
            raise DuplicateIntentError(rejection.reason)
        self._intent_ids.add(intent_id)
        return observed_at

    def _reject(
        self,
        intent_id: str,
        contract: str,
        operation: str,
        reason: str,
        timestamp: datetime,
        error_type: type[PositionManagerError],
    ) -> None:
        self._rejections.append(
            RejectedIntent(
                intent_id=intent_id,
                contract=contract,
                operation=operation,
                reason=reason,
                timestamp=timestamp,
            )
        )
        raise error_type(reason)

    def enter_position(
        self,
        *,
        intent_id: str,
        contract: str,
        side: ContractSide,
        quantity: float,
        price: float,
        timestamp: datetime,
        fee: float = 0.0,
    ) -> EntryRecord:
        """Accept one full entry, rejecting pyramids and implicit flips."""
        observed_at = self._begin_intent(intent_id, contract, "ENTRY", timestamp)
        self._validate_fill(quantity, price, fee)
        existing = self._positions.get(contract)
        if existing is not None:
            if existing.side is not side:
                self._reject(
                    intent_id,
                    contract,
                    "ENTRY",
                    "opposite position must be exited before entry",
                    observed_at,
                    PositionConflictError,
                )
            if not self.config.pyramiding_enabled:
                self._reject(
                    intent_id,
                    contract,
                    "ENTRY",
                    "pyramiding is not allowed",
                    observed_at,
                    PositionConflictError,
                )
            trades = self._trade_counts.get(contract, 0)
            if trades >= self.config.max_trades_per_contract:
                self._reject(
                    intent_id,
                    contract,
                    "ENTRY",
                    "maximum trades per contract reached",
                    observed_at,
                    PositionLimitError,
                )
            combined_qty = existing.quantity + quantity
            combined_price = (
                existing.entry_price * existing.quantity + price * quantity
            ) / combined_qty
            combined_fee = existing.entry_fee + fee
            record = EntryRecord(
                intent_id=intent_id,
                contract=contract,
                side=side,
                quantity=quantity,
                price=price,
                fee=fee,
                timestamp=observed_at,
            )
            self._positions[contract] = Position(
                contract=contract,
                side=side,
                quantity=combined_qty,
                entry_price=combined_price,
                entry_fee=combined_fee,
                opened_at=existing.opened_at,
                entry_intent_id=existing.entry_intent_id,
            )
            self._entries.append(record)
            self._trade_counts[contract] = trades + 1
            self._last_sides[contract] = side
            return record
        trades = self._trade_counts.get(contract, 0)
        if trades >= self.config.max_trades_per_contract:
            self._reject(
                intent_id,
                contract,
                "ENTRY",
                "maximum trades per contract reached",
                observed_at,
                PositionLimitError,
            )
        previous_side = self._last_sides.get(contract)
        is_flip = previous_side is not None and previous_side is not side
        flips = self._flip_counts.get(contract, 0)
        if is_flip and flips >= self.config.max_flips_per_contract:
            self._reject(
                intent_id,
                contract,
                "ENTRY",
                "maximum flips per contract reached",
                observed_at,
                PositionLimitError,
            )
        record = EntryRecord(
            intent_id=intent_id,
            contract=contract,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            timestamp=observed_at,
        )
        self._positions[contract] = Position(
            contract=contract,
            side=side,
            quantity=quantity,
            entry_price=price,
            entry_fee=fee,
            opened_at=observed_at,
            entry_intent_id=intent_id,
        )
        self._entries.append(record)
        self._trade_counts[contract] = trades + 1
        self._flip_counts[contract] = flips + int(is_flip)
        self._last_sides[contract] = side
        return record

    enter = enter_position

    def exit_position(
        self,
        *,
        intent_id: str,
        contract: str,
        price: float,
        timestamp: datetime,
        fee: float = 0.0,
        reason: str = "exit",
    ) -> ExitRecord:
        """Close an entire position at an externally supplied fill price."""
        observed_at = self._begin_intent(intent_id, contract, "EXIT", timestamp)
        position = self._positions.get(contract)
        if position is None:
            self._reject(
                intent_id,
                contract,
                "EXIT",
                "no open position",
                observed_at,
                PositionConflictError,
            )
        assert position is not None
        self._validate_fill(position.quantity, price, fee)
        if observed_at < position.opened_at:
            self._reject(
                intent_id,
                contract,
                "EXIT",
                "exit timestamp cannot precede entry timestamp",
                observed_at,
                PositionConflictError,
            )
        pnl = (
            (price - position.entry_price) * position.quantity
            - position.entry_fee
            - fee
        )
        record = ExitRecord(
            intent_id=intent_id,
            contract=contract,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            exit_price=price,
            entry_fee=position.entry_fee,
            exit_fee=fee,
            realized_pnl=pnl,
            timestamp=observed_at,
            reason=reason,
        )
        del self._positions[contract]
        self._exits.append(record)
        self._realized_pnl += pnl
        return record

    exit = exit_position

    def settle_position(
        self,
        *,
        intent_id: str,
        contract: str,
        winning_side: ContractSide,
        timestamp: datetime,
        fee: float = 0.0,
    ) -> ExitRecord:
        """Settle an open contract at its final binary payout."""
        position = self._positions.get(contract)
        if position is None:
            observed_at = self._begin_intent(intent_id, contract, "SETTLEMENT", timestamp)
            self._reject(
                intent_id,
                contract,
                "SETTLEMENT",
                "no open position",
                observed_at,
                PositionConflictError,
            )
        assert position is not None
        payout = 1.0 if position.side is winning_side else 0.0
        return self.exit_position(
            intent_id=intent_id,
            contract=contract,
            price=payout,
            timestamp=timestamp,
            fee=fee,
            reason="settlement",
        )

    settle = settle_position

    def unrealized_pnl(
        self,
        marks: Mapping[str, float] | str,
        price: float | None = None,
    ) -> float:
        """Mark open positions using contract-side prices supplied by caller.

        For convenience, pass either a ``contract -> held-side price`` mapping,
        or one contract plus ``price``.
        """
        if isinstance(marks, str):
            if price is None:
                raise ValueError("price is required when marks is a contract")
            mark_map: Mapping[str, float] = {marks: price}
        else:
            mark_map = marks
        total = 0.0
        for contract, position in self._positions.items():
            if contract not in mark_map:
                continue
            mark = float(mark_map[contract])
            if not math.isfinite(mark) or not 0.0 <= mark <= 1.0:
                raise ValueError("mark prices must be finite and within [0, 1]")
            total += (mark - position.entry_price) * position.quantity - position.entry_fee
        return total

    def export_state(self) -> dict[str, object]:
        """Return a JSON-serializable audit snapshot."""

        def encode(record: object) -> dict[str, object]:
            values = asdict(record)
            for key, value in tuple(values.items()):
                if isinstance(value, datetime):
                    values[key] = value.isoformat()
                elif isinstance(value, ContractSide):
                    values[key] = value.value
            return values

        return {
            "mode": self.mode,
            "realized_pnl": self._realized_pnl,
            "positions": {
                contract: encode(position)
                for contract, position in sorted(self._positions.items())
            },
            "entries": [encode(record) for record in self._entries],
            "exits": [encode(record) for record in self._exits],
            "rejections": [encode(record) for record in self._rejections],
            "trade_counts": dict(sorted(self._trade_counts.items())),
            "flip_counts": dict(sorted(self._flip_counts.items())),
            "intent_ids": sorted(self._intent_ids),
        }
