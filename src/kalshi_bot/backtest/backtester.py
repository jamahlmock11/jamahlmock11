"""Causal, chronological replay engine for binary contract decisions."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime

from kalshi_bot.backtest.performance import (
    PerformanceReport,
    TradeRecord,
    calculate_performance,
    time_to_expiry_bucket,
)
from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    DecisionResult,
    FeatureSnapshot,
    MarketPosition,
    MarketSnapshot,
    OrderBookSnapshot,
    ProbabilityEstimate,
    utc_datetime,
)
from kalshi_bot.execution.position_manager import (
    EntryRecord,
    ExitRecord,
    PositionManager,
    PositionManagerConfig,
    PositionManagerError,
)
from kalshi_bot.market.orderbook import InsufficientDepthError, estimate_buy_execution
from kalshi_bot.strategies.decision import DecisionConfig, DecisionEngine


class BacktestError(ValueError):
    """Base class for invalid or non-causal replay input."""


class ChronologyError(BacktestError):
    """Raised when replay rows are not chronological."""


class LookaheadError(BacktestError):
    """Raised when a replay input was unavailable at decision time."""


@dataclass(frozen=True)
class BacktestConfig:
    """Execution assumptions and lifecycle limits for deterministic replay."""

    default_quantity: float = 1.0
    fee_rate: float = 0.0
    fee_per_contract: float = 0.0
    slippage_bps: float = 0.0
    slippage_per_contract: float = 0.0
    max_flips_per_contract: int = 2
    max_trades_per_contract: int = 4
    require_final_settlement: bool = True

    def __post_init__(self) -> None:
        values = (
            self.default_quantity,
            self.fee_rate,
            self.fee_per_contract,
            self.slippage_bps,
            self.slippage_per_contract,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("execution configuration must be finite")
        if self.default_quantity <= 0:
            raise ValueError("default_quantity must be positive")
        if min(values[1:]) < 0:
            raise ValueError("fees and slippage cannot be negative")


@dataclass(frozen=True)
class BacktestEvent:
    """All data known for one decision timestamp."""

    decision_time: datetime
    market: MarketSnapshot | None = None
    orderbook: OrderBookSnapshot | None = None
    benchmark: BenchmarkQuote | None = None
    features: FeatureSnapshot | None = None
    forecast: ProbabilityEstimate | None = None
    decision: DecisionResult | None = None
    data_timestamps: Mapping[str, datetime] = field(default_factory=dict)
    settlement_outcome: ContractSide | bool | None = None
    signal: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_time", utc_datetime(self.decision_time))


@dataclass(frozen=True)
class PipelineResult:
    """Optional values produced causally by a replay callback."""

    market: MarketSnapshot | None = None
    orderbook: OrderBookSnapshot | None = None
    benchmark: BenchmarkQuote | None = None
    features: FeatureSnapshot | None = None
    forecast: ProbabilityEstimate | None = None
    decision: DecisionResult | None = None
    signal: str | None = None


@dataclass(frozen=True)
class Settlement:
    """Explicit final binary result and the time it became known."""

    winning_side: ContractSide
    timestamp: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", utc_datetime(self.timestamp))


@dataclass(frozen=True)
class DecisionJournalRecord:
    """Auditable result for every decision, including no-trade outcomes."""

    timestamp: datetime
    contract: str
    action: DecisionAction
    status: str
    reason: str
    rejected: bool
    selected_side: ContractSide | None
    quantity: float
    fill_price: float | None
    fee: float
    predicted_probability: float | None
    predicted_edge: float | None
    outcome: float | None
    regime: str | None
    trajectory: str | None
    signal: str | None
    time_to_expiry: float | None
    time_to_expiry_bucket: str

    @property
    def pnl(self) -> None:
        """Decision rows do not duplicate lifecycle P&L."""
        return None


@dataclass(frozen=True)
class BacktestResult:
    """Complete replay output."""

    decisions: tuple[DecisionJournalRecord, ...]
    trades: tuple[TradeRecord, ...]
    metrics: PerformanceReport
    position_state: Mapping[str, object]


PipelineCallback = Callable[
    [BacktestEvent],
    PipelineResult | DecisionResult | Mapping[str, object],
]
ReplayRow = BacktestEvent | Mapping[str, object]
SettlementValue = Settlement | ContractSide | bool


@dataclass(frozen=True)
class _EntryContext:
    record: EntryRecord
    predicted_probability: float | None
    predicted_edge: float | None
    regime: str | None
    trajectory: str | None
    time_to_expiry: float | None


def _enum_label(value: object | None) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _event_from_mapping(row: Mapping[str, object]) -> BacktestEvent:
    if "decision_time" not in row:
        raise BacktestError("replay row is missing decision_time")
    decision_time = row["decision_time"]
    if not isinstance(decision_time, datetime):
        raise BacktestError("decision_time must be a datetime")
    data_timestamps = row.get("data_timestamps", {})
    if not isinstance(data_timestamps, Mapping):
        raise BacktestError("data_timestamps must be a mapping")
    typed_timestamps: dict[str, datetime] = {}
    for name, timestamp in data_timestamps.items():
        if not isinstance(timestamp, datetime):
            raise BacktestError(f"data timestamp {name!r} must be a datetime")
        typed_timestamps[str(name)] = timestamp
    return BacktestEvent(
        decision_time=decision_time,
        market=row.get("market") if isinstance(row.get("market"), MarketSnapshot) else None,
        orderbook=(
            row.get("orderbook")
            if isinstance(row.get("orderbook"), OrderBookSnapshot)
            else None
        ),
        benchmark=(
            row.get("benchmark")
            if isinstance(row.get("benchmark"), BenchmarkQuote)
            else None
        ),
        features=(
            row.get("features")
            if isinstance(row.get("features"), FeatureSnapshot)
            else None
        ),
        forecast=(
            row.get("forecast")
            if isinstance(row.get("forecast"), ProbabilityEstimate)
            else None
        ),
        decision=(
            row.get("decision")
            if isinstance(row.get("decision"), DecisionResult)
            else None
        ),
        data_timestamps=typed_timestamps,
        settlement_outcome=(
            row.get("settlement_outcome")
            if isinstance(row.get("settlement_outcome"), (ContractSide, bool))
            else None
        ),
        signal=str(row["signal"]) if row.get("signal") is not None else None,
    )


class Backtester:
    """Replay snapshots in input order with depth-aware execution.

    The callback receives exactly one current event. Its returned timestamps
    are validated before any decision is accepted. Opposite-side entries are
    rejected while a position is open, so a flip necessarily spans a separate
    exit action and later entry action.
    """

    def __init__(
        self,
        config: BacktestConfig | None = None,
        *,
        decision_engine: DecisionEngine | None = None,
        pipeline: PipelineCallback | None = None,
    ) -> None:
        self.config = config or BacktestConfig()
        self.decision_engine = decision_engine or DecisionEngine(
            DecisionConfig(allow_replay_data=True)
        )
        self.pipeline = pipeline
        self.position_manager = self._new_position_manager()

    def _new_position_manager(self) -> PositionManager:
        return PositionManager(
            PositionManagerConfig(
                max_flips_per_contract=self.config.max_flips_per_contract,
                max_trades_per_contract=self.config.max_trades_per_contract,
            ),
            mode="backtest",
        )

    @staticmethod
    def _validate_event_timestamps(event: BacktestEvent) -> None:
        decision_time = event.decision_time
        timestamps: dict[str, datetime] = dict(event.data_timestamps)
        if event.orderbook is not None:
            timestamps["orderbook"] = event.orderbook.timestamp
        if event.market is not None:
            timestamps["market_orderbook"] = event.market.orderbook.timestamp
        if event.benchmark is not None:
            timestamps["benchmark"] = event.benchmark.timestamp
        if event.features is not None:
            timestamps["features"] = event.features.timestamp
        for name, timestamp in timestamps.items():
            observed_at = utc_datetime(timestamp)
            if observed_at > decision_time:
                raise LookaheadError(
                    f"{name} timestamp {observed_at.isoformat()} is later than "
                    f"decision time {decision_time.isoformat()}"
                )

    @staticmethod
    def _validate_chronology(events: list[BacktestEvent]) -> None:
        for previous, current in zip(events, events[1:]):
            if current.decision_time < previous.decision_time:
                raise ChronologyError(
                    "replay rows must be sorted by nondecreasing decision_time"
                )

    def _with_position(self, event: BacktestEvent) -> BacktestEvent:
        market = event.market
        if market is None:
            return event
        book = event.orderbook or market.orderbook
        position = self.position_manager.position(market.ticker)
        market_position = (
            MarketPosition(
                side=position.side,
                quantity=position.quantity,
                average_price=position.entry_price,
            )
            if position is not None
            else None
        )
        return replace(
            event,
            market=replace(
                market,
                orderbook=book,
                current_position=market_position,
            ),
            orderbook=book,
        )

    @staticmethod
    def _pipeline_result(
        output: PipelineResult | DecisionResult | Mapping[str, object],
    ) -> PipelineResult:
        if isinstance(output, PipelineResult):
            return output
        if isinstance(output, DecisionResult):
            return PipelineResult(decision=output)
        if not isinstance(output, Mapping):
            raise BacktestError("pipeline must return PipelineResult, DecisionResult, or mapping")
        return PipelineResult(
            market=(
                output.get("market")
                if isinstance(output.get("market"), MarketSnapshot)
                else None
            ),
            orderbook=(
                output.get("orderbook")
                if isinstance(output.get("orderbook"), OrderBookSnapshot)
                else None
            ),
            benchmark=(
                output.get("benchmark")
                if isinstance(output.get("benchmark"), BenchmarkQuote)
                else None
            ),
            features=(
                output.get("features")
                if isinstance(output.get("features"), FeatureSnapshot)
                else None
            ),
            forecast=(
                output.get("forecast")
                if isinstance(output.get("forecast"), ProbabilityEstimate)
                else None
            ),
            decision=(
                output.get("decision")
                if isinstance(output.get("decision"), DecisionResult)
                else None
            ),
            signal=str(output["signal"]) if output.get("signal") is not None else None,
        )

    def _resolve(self, event: BacktestEvent) -> BacktestEvent:
        current = self._with_position(event)
        if self.pipeline is not None:
            output = self._pipeline_result(self.pipeline(current))
            current = replace(
                current,
                market=output.market or current.market,
                orderbook=output.orderbook or current.orderbook,
                benchmark=output.benchmark or current.benchmark,
                features=output.features or current.features,
                forecast=output.forecast or current.forecast,
                decision=output.decision or current.decision,
                signal=output.signal or current.signal,
            )
            current = self._with_position(current)
        self._validate_event_timestamps(current)
        if current.decision is not None:
            return current
        if (
            current.market is None
            or current.forecast is None
            or current.features is None
            or current.benchmark is None
        ):
            raise BacktestError(
                "each decision requires market, benchmark, features, and forecast "
                "unless the pipeline returns a decision"
            )
        decision = self.decision_engine.decide(
            current.market,
            current.forecast,
            current.features,
            current.benchmark,
            now=current.decision_time,
        )
        return replace(current, decision=decision)

    def _sell_fill(
        self,
        book: OrderBookSnapshot,
        side: ContractSide,
        quantity: float,
    ) -> tuple[float, float]:
        remaining = quantity
        proceeds = 0.0
        filled = 0.0
        for level in book.levels(side, asks=False):
            take = min(remaining, level.size)
            if take <= 0:
                continue
            proceeds += take * level.price
            filled += take
            remaining -= take
            if remaining <= 1e-12:
                break
        if filled <= 0 or remaining > 1e-12:
            raise InsufficientDepthError(
                f"{side.value} bids fill {filled:g} of requested {quantity:g}"
            )
        average = proceeds / filled
        slippage = (
            self.config.slippage_per_contract
            + average * self.config.slippage_bps / 10_000.0
        )
        net_price = max(0.0, average - slippage)
        fee_each = (
            self.config.fee_per_contract
            + self.config.fee_rate * average * (1.0 - average)
        )
        return net_price, fee_each * filled

    @staticmethod
    def _prediction(event: BacktestEvent) -> tuple[ContractSide | None, float | None]:
        assert event.decision is not None
        side = event.decision.selected_side
        probability = event.decision.predicted_probability
        if event.forecast is not None and (side is None or probability is None):
            side = (
                ContractSide.YES
                if event.forecast.p_up >= event.forecast.p_down
                else ContractSide.NO
            )
            probability = (
                event.forecast.p_up if side is ContractSide.YES else event.forecast.p_down
            )
        return side, probability

    def _journal(
        self,
        event: BacktestEvent,
        *,
        status: str,
        reason: str,
        rejected: bool,
        fill_price: float | None = None,
        fee: float = 0.0,
    ) -> DecisionJournalRecord:
        assert event.decision is not None
        side, probability = self._prediction(event)
        market = event.market
        time_remaining = (
            (market.expiration - event.decision_time).total_seconds()
            if market is not None
            else (
                event.features.seconds_remaining if event.features is not None else None
            )
        )
        return DecisionJournalRecord(
            timestamp=event.decision_time,
            contract=market.ticker if market is not None else "UNKNOWN",
            action=event.decision.action,
            status=status,
            reason=reason,
            rejected=rejected,
            selected_side=side,
            quantity=event.decision.quantity,
            fill_price=fill_price,
            fee=fee,
            predicted_probability=probability,
            predicted_edge=event.decision.edge,
            outcome=None,
            regime=_enum_label(event.forecast.regime) if event.forecast is not None else None,
            trajectory=(
                _enum_label(event.features.trajectory)
                if event.features is not None
                else None
            ),
            signal=event.signal or (_enum_label(side) if side is not None else None),
            time_to_expiry=time_remaining,
            time_to_expiry_bucket=time_to_expiry_bucket(time_remaining),
        )

    def _trade_from_exit(
        self,
        exit_record: ExitRecord,
        context: _EntryContext,
    ) -> TradeRecord:
        return TradeRecord(
            contract=exit_record.contract,
            side=exit_record.side,
            quantity=exit_record.quantity,
            entry_price=exit_record.entry_price,
            exit_price=exit_record.exit_price,
            entry_timestamp=context.record.timestamp,
            exit_timestamp=exit_record.timestamp,
            fees=exit_record.entry_fee + exit_record.exit_fee,
            pnl=exit_record.realized_pnl,
            predicted_probability=context.predicted_probability,
            predicted_edge=context.predicted_edge,
            outcome=None,
            regime=context.regime,
            time_to_expiry=context.time_to_expiry,
            exit_reason=exit_record.reason,
        )

    def _execute(
        self,
        event: BacktestEvent,
        index: int,
        entries: dict[str, _EntryContext],
    ) -> tuple[DecisionJournalRecord, TradeRecord | None]:
        assert event.decision is not None
        decision = event.decision
        action = decision.action
        if action in {DecisionAction.NO_TRADE, DecisionAction.HOLD}:
            status = "no_trade" if action is DecisionAction.NO_TRADE else "hold"
            return self._journal(
                event,
                status=status,
                reason=decision.reason,
                rejected=False,
            ), None
        if event.market is None:
            return self._journal(
                event,
                status="rejected",
                reason="executable decision requires a market",
                rejected=True,
            ), None
        contract = event.market.ticker
        book = event.orderbook or event.market.orderbook
        intent_id = f"backtest-{index}-{action.value.lower()}"
        side, probability = self._prediction(event)
        try:
            if action in {DecisionAction.BUY_UP, DecisionAction.BUY_DOWN}:
                expected_side = (
                    ContractSide.YES
                    if action is DecisionAction.BUY_UP
                    else ContractSide.NO
                )
                if side is not None and side is not expected_side:
                    raise BacktestError("decision action and selected_side disagree")
                side = expected_side
                quantity = decision.quantity or self.config.default_quantity
                execution = estimate_buy_execution(
                    book,
                    side,
                    quantity,
                    fee_rate=self.config.fee_rate,
                    fee_per_contract=self.config.fee_per_contract,
                    slippage_bps=self.config.slippage_bps,
                    slippage_per_contract=self.config.slippage_per_contract,
                )
                entry_price = execution.average_price + execution.slippage_per_contract
                fee = execution.fee_per_contract * execution.filled_quantity
                record = self.position_manager.enter_position(
                    intent_id=intent_id,
                    contract=contract,
                    side=side,
                    quantity=execution.filled_quantity,
                    price=entry_price,
                    fee=fee,
                    timestamp=event.decision_time,
                )
                entries[contract] = _EntryContext(
                    record=record,
                    predicted_probability=probability,
                    predicted_edge=decision.edge,
                    regime=(
                        _enum_label(event.forecast.regime)
                        if event.forecast is not None
                        else None
                    ),
                    trajectory=(
                        _enum_label(event.features.trajectory)
                        if event.features is not None
                        else None
                    ),
                    time_to_expiry=(
                        event.features.seconds_remaining
                        if event.features is not None
                        else (event.market.expiration - event.decision_time).total_seconds()
                    ),
                )
                return self._journal(
                    event,
                    status="filled",
                    reason=decision.reason,
                    rejected=False,
                    fill_price=entry_price,
                    fee=fee,
                ), None
            if action is DecisionAction.EXIT:
                position = self.position_manager.position(contract)
                if position is None:
                    raise BacktestError("cannot exit without an open position")
                exit_price, fee = self._sell_fill(book, position.side, position.quantity)
                exit_record = self.position_manager.exit_position(
                    intent_id=intent_id,
                    contract=contract,
                    price=exit_price,
                    fee=fee,
                    timestamp=event.decision_time,
                    reason="decision_exit",
                )
                context = entries.pop(contract)
                trade = self._trade_from_exit(exit_record, context)
                return self._journal(
                    event,
                    status="filled",
                    reason=decision.reason,
                    rejected=False,
                    fill_price=exit_price,
                    fee=fee,
                ), trade
            raise BacktestError(f"unsupported decision action {action.value}")
        except (BacktestError, InsufficientDepthError, PositionManagerError, ValueError) as exc:
            return self._journal(
                event,
                status="rejected",
                reason=str(exc),
                rejected=True,
            ), None

    @staticmethod
    def _winning_side(value: ContractSide | bool) -> ContractSide:
        if isinstance(value, ContractSide):
            return value
        return ContractSide.YES if value else ContractSide.NO

    def run(
        self,
        rows: Iterable[ReplayRow],
        *,
        settlements: Mapping[str, SettlementValue] | None = None,
    ) -> BacktestResult:
        """Replay rows and settle every remaining position from explicit labels."""
        events = [
            row if isinstance(row, BacktestEvent) else _event_from_mapping(row)
            for row in rows
        ]
        self._validate_chronology(events)
        for event in events:
            self._validate_event_timestamps(event)
        self.position_manager = self._new_position_manager()
        decisions: list[DecisionJournalRecord] = []
        trades: list[TradeRecord] = []
        entries: dict[str, _EntryContext] = {}
        outcomes: dict[str, Settlement] = {}

        for index, raw_event in enumerate(events):
            event = self._resolve(raw_event)
            if event.settlement_outcome is not None and event.market is not None:
                outcomes[event.market.ticker] = Settlement(
                    winning_side=self._winning_side(event.settlement_outcome),
                    timestamp=event.decision_time,
                )
            journal, trade = self._execute(event, index, entries)
            decisions.append(journal)
            if trade is not None:
                trades.append(trade)

        for contract, value in (settlements or {}).items():
            if isinstance(value, Settlement):
                settlement = value
            else:
                fallback_time = events[-1].decision_time if events else None
                if fallback_time is None:
                    raise BacktestError(
                        "a timestamped Settlement is required when replay has no rows"
                    )
                settlement = Settlement(
                    winning_side=self._winning_side(value),
                    timestamp=fallback_time,
                )
            outcomes[contract] = settlement

        for contract in tuple(self.position_manager.positions):
            settlement = outcomes.get(contract)
            if settlement is None:
                if self.config.require_final_settlement:
                    raise BacktestError(
                        f"open position {contract!r} has no explicit final settlement"
                    )
                continue
            exit_record = self.position_manager.settle_position(
                intent_id=f"backtest-settlement-{contract}",
                contract=contract,
                winning_side=settlement.winning_side,
                timestamp=settlement.timestamp,
            )
            context = entries.pop(contract)
            trades.append(self._trade_from_exit(exit_record, context))

        resolved_trades: list[TradeRecord] = []
        for trade in trades:
            settlement = outcomes.get(trade.contract)
            outcome = (
                1.0 if settlement is not None and trade.side is settlement.winning_side else 0.0
                if settlement is not None
                else None
            )
            resolved_trades.append(replace(trade, outcome=outcome))
        resolved_decisions: list[DecisionJournalRecord] = []
        for decision in decisions:
            settlement = outcomes.get(decision.contract)
            outcome = (
                1.0
                if settlement is not None
                and decision.selected_side is settlement.winning_side
                else 0.0
                if settlement is not None and decision.selected_side is not None
                else None
            )
            resolved_decisions.append(replace(decision, outcome=outcome))
        metrics = calculate_performance(resolved_trades, resolved_decisions)
        return BacktestResult(
            decisions=tuple(resolved_decisions),
            trades=tuple(resolved_trades),
            metrics=metrics,
            position_state=self.position_manager.export_state(),
        )


def run_backtest(
    rows: Iterable[ReplayRow],
    *,
    config: BacktestConfig | None = None,
    decision_engine: DecisionEngine | None = None,
    pipeline: PipelineCallback | None = None,
    settlements: Mapping[str, SettlementValue] | None = None,
) -> BacktestResult:
    """Run one deterministic replay with a compact functional API."""
    return Backtester(
        config,
        decision_engine=decision_engine,
        pipeline=pipeline,
    ).run(rows, settlements=settlements)
