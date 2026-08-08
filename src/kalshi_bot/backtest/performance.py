"""Numerically robust performance summaries for replayed binary trades."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from kalshi_bot.domain import ContractSide


@dataclass(frozen=True)
class TradeRecord:
    """A completed entry-to-exit or entry-to-settlement lifecycle."""

    contract: str
    side: ContractSide
    quantity: float
    entry_price: float
    exit_price: float
    entry_timestamp: datetime
    exit_timestamp: datetime
    fees: float
    pnl: float
    predicted_probability: float | None = None
    predicted_edge: float | None = None
    outcome: float | None = None
    regime: str | None = None
    time_to_expiry: float | None = None
    exit_reason: str = "exit"

    @property
    def realized_edge(self) -> float:
        """Return payout/exit value less entry price, per contract."""
        terminal_value = self.outcome if self.outcome is not None else self.exit_price
        return terminal_value - self.entry_price


@dataclass(frozen=True)
class PerformanceMetrics:
    """Aggregate metrics for one complete or segmented sample."""

    trade_count: int = 0
    rejected_count: int = 0
    win_rate: float = 0.0
    average_win: float = 0.0
    average_loss: float = 0.0
    profit_factor: float = 0.0
    expected_value: float = 0.0
    max_drawdown: float = 0.0
    sharpe_like: float = 0.0
    average_predicted_edge: float = 0.0
    realized_edge: float = 0.0
    brier_score: float = 0.0
    calibration_error: float = 0.0
    prediction_count: int = 0
    total_pnl: float = 0.0


@dataclass(frozen=True)
class PerformanceReport:
    """Overall metrics plus regime and expiry-horizon slices."""

    overall: PerformanceMetrics
    by_regime: Mapping[str, PerformanceMetrics]
    by_time_to_expiry: Mapping[str, PerformanceMetrics]


JournalRecord = TradeRecord | Mapping[str, object] | object


def _value(record: JournalRecord, name: str, default: object = None) -> object:
    if isinstance(record, Mapping):
        return record.get(name, default)
    return getattr(record, name, default)


def _number(record: JournalRecord, name: str) -> float | None:
    value = _value(record, name)
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _label(record: JournalRecord, name: str, default: str = "UNKNOWN") -> str:
    value = _value(record, name)
    if value is None:
        return default
    enum_value = getattr(value, "value", value)
    return str(enum_value)


def time_to_expiry_bucket(
    seconds: float | None,
    boundaries: Sequence[float] = (60.0, 300.0, 900.0),
) -> str:
    """Map seconds remaining to a deterministic inclusive horizon bucket."""
    if seconds is None or not math.isfinite(seconds):
        return "UNKNOWN"
    if any(boundary < 0 for boundary in boundaries):
        raise ValueError("expiry bucket boundaries cannot be negative")
    previous = 0.0
    for boundary in sorted(boundaries):
        if seconds <= boundary:
            return f"{previous:g}-{boundary:g}s"
        previous = boundary
    return f">{previous:g}s"


def _is_rejected(record: JournalRecord) -> bool:
    rejected = _value(record, "rejected", False)
    if bool(rejected):
        return True
    status = str(_value(record, "status", "")).lower()
    return status in {"rejected", "failed"}


def _calibration(
    records: Iterable[JournalRecord],
    *,
    n_bins: int = 10,
) -> tuple[int, float, float]:
    pairs: list[tuple[float, float]] = []
    for record in records:
        probability = _number(record, "predicted_probability")
        outcome = _number(record, "outcome")
        if probability is None or outcome is None:
            continue
        if 0.0 <= probability <= 1.0 and outcome in (0.0, 1.0):
            pairs.append((probability, outcome))
    if not pairs:
        return 0, 0.0, 0.0
    brier = sum((probability - outcome) ** 2 for probability, outcome in pairs) / len(pairs)
    bins: list[list[tuple[float, float]]] = [[] for _ in range(n_bins)]
    for probability, outcome in pairs:
        bins[min(int(probability * n_bins), n_bins - 1)].append((probability, outcome))
    error = 0.0
    for entries in bins:
        if not entries:
            continue
        mean_probability = sum(item[0] for item in entries) / len(entries)
        frequency = sum(item[1] for item in entries) / len(entries)
        error += len(entries) / len(pairs) * abs(mean_probability - frequency)
    return len(pairs), brier, error


def _metrics(
    trades: Sequence[JournalRecord],
    decisions: Sequence[JournalRecord],
) -> PerformanceMetrics:
    pnls = [value for trade in trades if (value := _number(trade, "pnl")) is not None]
    wins = [value for value in pnls if value > 0.0]
    losses = [value for value in pnls if value < 0.0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    if gross_loss > 0.0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0.0:
        profit_factor = math.inf
    else:
        profit_factor = 0.0

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)

    mean_pnl = sum(pnls) / len(pnls) if pnls else 0.0
    if len(pnls) >= 2:
        variance = sum((value - mean_pnl) ** 2 for value in pnls) / (len(pnls) - 1)
        standard_deviation = math.sqrt(max(variance, 0.0))
        sharpe_like = mean_pnl / standard_deviation if standard_deviation > 0.0 else 0.0
    else:
        sharpe_like = 0.0

    predicted_edges = [
        value
        for trade in trades
        if (value := _number(trade, "predicted_edge")) is not None
    ]
    realized_edges: list[float] = []
    for trade in trades:
        explicit = _number(trade, "realized_edge")
        if explicit is not None:
            realized_edges.append(explicit)
            continue
        outcome = _number(trade, "outcome")
        entry = _number(trade, "entry_price")
        exit_price = _number(trade, "exit_price")
        terminal = outcome if outcome is not None else exit_price
        if terminal is not None and entry is not None:
            realized_edges.append(terminal - entry)

    calibration_source = decisions if decisions else trades
    prediction_count, brier, calibration_error = _calibration(calibration_source)
    return PerformanceMetrics(
        trade_count=len(trades),
        rejected_count=sum(_is_rejected(decision) for decision in decisions),
        win_rate=len(wins) / len(pnls) if pnls else 0.0,
        average_win=sum(wins) / len(wins) if wins else 0.0,
        average_loss=sum(losses) / len(losses) if losses else 0.0,
        profit_factor=profit_factor,
        expected_value=mean_pnl,
        max_drawdown=max_drawdown,
        sharpe_like=sharpe_like,
        average_predicted_edge=(
            sum(predicted_edges) / len(predicted_edges) if predicted_edges else 0.0
        ),
        realized_edge=sum(realized_edges) / len(realized_edges) if realized_edges else 0.0,
        brier_score=brier,
        calibration_error=calibration_error,
        prediction_count=prediction_count,
        total_pnl=sum(pnls),
    )


def calculate_performance(
    trades: Iterable[JournalRecord],
    decisions: Iterable[JournalRecord] = (),
    *,
    expiry_boundaries: Sequence[float] = (60.0, 300.0, 900.0),
) -> PerformanceReport:
    """Calculate overall and segmented metrics without division failures."""
    trade_list = list(trades)
    decision_list = list(decisions)
    regimes = sorted(
        {
            _label(record, "regime")
            for record in (*trade_list, *decision_list)
            if _label(record, "regime") != "UNKNOWN"
        }
    )
    by_regime: dict[str, PerformanceMetrics] = {}
    for regime in regimes:
        regime_trades = [record for record in trade_list if _label(record, "regime") == regime]
        regime_decisions = [
            record for record in decision_list if _label(record, "regime") == regime
        ]
        by_regime[regime] = _metrics(regime_trades, regime_decisions)

    def bucket_for(record: JournalRecord) -> str:
        explicit = _value(record, "time_to_expiry_bucket")
        if explicit is not None:
            return str(explicit)
        return time_to_expiry_bucket(_number(record, "time_to_expiry"), expiry_boundaries)

    buckets = sorted(
        {
            bucket_for(record)
            for record in (*trade_list, *decision_list)
            if bucket_for(record) != "UNKNOWN"
        }
    )
    by_expiry: dict[str, PerformanceMetrics] = {}
    for bucket in buckets:
        bucket_trades = [record for record in trade_list if bucket_for(record) == bucket]
        bucket_decisions = [
            record for record in decision_list if bucket_for(record) == bucket
        ]
        by_expiry[bucket] = _metrics(bucket_trades, bucket_decisions)
    return PerformanceReport(
        overall=_metrics(trade_list, decision_list),
        by_regime=by_regime,
        by_time_to_expiry=by_expiry,
    )


compute_performance = calculate_performance
