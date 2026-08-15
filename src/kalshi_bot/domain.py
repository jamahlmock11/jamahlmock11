"""Shared immutable domain objects for the BRTI forecasting core."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


def utc_datetime(value: datetime) -> datetime:
    """Return an aware UTC datetime, rejecting ambiguous naive timestamps."""
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"


class ContractSide(str, Enum):
    YES = "YES"
    NO = "NO"


class TrajectoryState(str, Enum):
    ACCELERATING_UP = "ACCELERATING_UP"
    DECELERATING_UP = "DECELERATING_UP"
    ACCELERATING_DOWN = "ACCELERATING_DOWN"
    DECELERATING_DOWN = "DECELERATING_DOWN"
    FLAT = "FLAT"
    REVERSING_UP = "REVERSING_UP"
    REVERSING_DOWN = "REVERSING_DOWN"


class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    BREAKOUT = "BREAKOUT"
    BREAKDOWN = "BREAKDOWN"
    REVERSAL_UP = "REVERSAL_UP"
    REVERSAL_DOWN = "REVERSAL_DOWN"
    CHAOTIC_UNSTABLE = "CHAOTIC_UNSTABLE"
    CHOPPY = "CHOPPY"
    UNCERTAIN = "UNCERTAIN"


class TrendClassification(str, Enum):
    STRONG_UP = "STRONG_UP"
    UP = "UP"
    WEAK_UP = "WEAK_UP"
    NEUTRAL = "NEUTRAL"
    WEAK_DOWN = "WEAK_DOWN"
    DOWN = "DOWN"
    STRONG_DOWN = "STRONG_DOWN"


class TradeTier(str, Enum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    NONE = "NONE"


class EntryTiming(str, Enum):
    EARLY = "EARLY"
    DEVELOPING = "DEVELOPING"
    CONFIRMED = "CONFIRMED"
    LATE = "LATE"


class DecisionAction(str, Enum):
    BUY_UP = "BUY_UP"
    BUY_DOWN = "BUY_DOWN"
    HOLD = "HOLD"
    EXIT = "EXIT"
    NO_TRADE = "NO_TRADE"


@dataclass(frozen=True)
class BenchmarkQuote:
    """An official, replay, or explicitly labelled BRTI proxy observation."""

    price: float
    timestamp: datetime
    source: str
    primary: bool = True
    is_live: bool = True
    replay: bool = False
    is_proxy: bool = False
    constituent_count: int = 0
    dispersion: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc_datetime(self.timestamp))
        if self.is_proxy and self.primary:
            raise ValueError("an unofficial proxy cannot be marked primary")
        if not self.primary and not self.is_proxy:
            raise ValueError("non-primary benchmark quotes must be explicit proxies")
        if self.replay and self.is_live:
            raise ValueError("a replay quote cannot be marked live")


@dataclass(frozen=True)
class SupportingQuote:
    """A non-settlement BTC/USD quote used only for corroboration."""

    price: float
    timestamp: datetime
    source: str
    bid: float | None = None
    ask: float | None = None
    healthy: bool = True
    primary: bool = False
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc_datetime(self.timestamp))
        if self.primary:
            raise ValueError("supporting feeds can never be primary")


@dataclass(frozen=True)
class SupportingAggregate:
    price: float
    timestamp: datetime
    quotes: tuple[SupportingQuote, ...]
    dispersion: float
    healthy_venues: int
    required_venues: int
    primary: bool = False

    @property
    def healthy(self) -> bool:
        return self.healthy_venues >= self.required_venues


@dataclass(frozen=True)
class OrderLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderBookSnapshot:
    timestamp: datetime
    yes_bids: tuple[OrderLevel, ...] = ()
    yes_asks: tuple[OrderLevel, ...] = ()
    no_bids: tuple[OrderLevel, ...] = ()
    no_asks: tuple[OrderLevel, ...] = ()

    @property
    def yes_bid(self) -> float | None:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def yes_ask(self) -> float | None:
        return self.yes_asks[0].price if self.yes_asks else None

    @property
    def no_bid(self) -> float | None:
        return self.no_bids[0].price if self.no_bids else None

    @property
    def no_ask(self) -> float | None:
        return self.no_asks[0].price if self.no_asks else None

    @property
    def yes_spread(self) -> float | None:
        return None if self.yes_bid is None or self.yes_ask is None else self.yes_ask - self.yes_bid

    @property
    def no_spread(self) -> float | None:
        return None if self.no_bid is None or self.no_ask is None else self.no_ask - self.no_bid

    def levels(self, side: ContractSide, *, asks: bool) -> tuple[OrderLevel, ...]:
        if side is ContractSide.YES:
            return self.yes_asks if asks else self.yes_bids
        return self.no_asks if asks else self.no_bids


@dataclass(frozen=True)
class MarketPosition:
    side: ContractSide
    quantity: float
    average_price: float = 0.0
    opened_at: datetime | None = None


@dataclass(frozen=True)
class OpenOrder:
    order_id: str
    side: ContractSide
    quantity: float
    price: float
    status: str = "open"


@dataclass(frozen=True)
class MarketSnapshot:
    ticker: str
    status: str
    rules: str
    strike: float
    expiration: datetime
    open_time: datetime
    reference: str
    orderbook: OrderBookSnapshot
    current_position: MarketPosition | None = None
    open_orders: tuple[OpenOrder, ...] = ()
    valid: bool = True
    rejection_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "expiration", utc_datetime(self.expiration))
        object.__setattr__(self, "open_time", utc_datetime(self.open_time))

    @property
    def open(self) -> datetime:
        """Explicit market-open timestamp (convenient domain alias)."""
        return self.open_time

    @property
    def yes_bid(self) -> float | None:
        return self.orderbook.yes_bid

    @property
    def yes_ask(self) -> float | None:
        return self.orderbook.yes_ask

    @property
    def no_bid(self) -> float | None:
        return self.orderbook.no_bid

    @property
    def no_ask(self) -> float | None:
        return self.orderbook.no_ask

    @property
    def yes_depth(self) -> float:
        return sum(level.size for level in self.orderbook.yes_asks)

    @property
    def no_depth(self) -> float:
        return sum(level.size for level in self.orderbook.no_asks)


@dataclass(frozen=True, order=True)
class RollingPricePoint:
    timestamp: datetime
    price: float
    source: str
    primary: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc_datetime(self.timestamp))


@dataclass(frozen=True)
class FeatureSnapshot:
    timestamp: datetime
    current_price: float
    strike: float
    seconds_remaining: float
    changes: Mapping[int, float]
    velocities: Mapping[int, float]
    acceleration: float
    short_trend: float
    medium_trend: float
    realized_vol: float
    expected_remaining_move: float
    z_distance_to_strike: float
    mean_reversion_score: float
    orderbook_imbalance: float
    cross_venue_agreement: float
    cross_venue_dispersion: float
    data_completeness: float
    trajectory: TrajectoryState
    sample_count: int
    oldest_sample_age: float
    rationale: Mapping[str, str] = field(default_factory=dict)
    yes_top_skew: float = 0.0
    settlement_effective_strike: float | None = None
    settlement_locked_fraction: float = 0.0
    late_momentum_pattern: str = "none"
    late_momentum_drift: float = 0.0
    late_momentum_hammer: float = 0.0
    late_momentum_fade: float = 0.0
    late_momentum_finish_bias: float = 0.0
    late_momentum_summary: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc_datetime(self.timestamp))


@dataclass(frozen=True)
class ProbabilityEstimate:
    p_up: float
    p_down: float
    confidence: float
    signal_agreement: float
    component_probabilities: Mapping[str, float]
    regime: Regime
    raw_p_up: float
    calibrated: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionEstimate:
    side: ContractSide
    quantity: float
    filled_quantity: float
    average_price: float
    fee_per_contract: float
    slippage_per_contract: float
    total_cost: float
    executable_cost: float
    levels_consumed: int

    @property
    def fully_filled(self) -> bool:
        return self.filled_quantity >= self.quantity


@dataclass(frozen=True)
class GateFailure:
    gate: str
    reason: str
    observed: Any = None
    required: Any = None


@dataclass(frozen=True)
class DecisionResult:
    action: DecisionAction
    reason: str
    gate_failures: tuple[GateFailure, ...]
    current_direction: Direction
    predicted_direction: Direction
    trade_direction: Direction
    selected_side: ContractSide | None = None
    predicted_probability: float | None = None
    executable_cost: float | None = None
    edge: float | None = None
    target_edge: float = 0.25
    required_edge: float | None = None
    trade_tier: TradeTier = TradeTier.NONE
    entry_timing: EntryTiming | None = None
    size_multiplier: float = 1.0
    quantity: float = 0.0
    execution: ExecutionEstimate | None = None
