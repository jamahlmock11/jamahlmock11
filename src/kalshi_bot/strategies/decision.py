"""Safety-gated trading decisions using executable, all-in contract costs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    DecisionResult,
    Direction,
    FeatureSnapshot,
    GateFailure,
    MarketSnapshot,
    ProbabilityEstimate,
    utc_datetime,
)
from kalshi_bot.market.orderbook import (
    InsufficientDepthError,
    depth,
    estimate_buy_execution,
    spread,
)

ABSOLUTE_MINIMUM_EDGE = Decimal("0.20")
EDGE_TOLERANCE = Decimal("0.000000000001")


@dataclass(frozen=True)
class DecisionConfig:
    minimum_edge: float = 0.20
    target_edge: float = 0.25
    quantity: float = 1.0
    maximum_benchmark_age: float = 15.0
    maximum_feature_age: float = 10.0
    minimum_seconds_remaining: float = 30.0
    maximum_seconds_remaining: float = 15 * 60.0
    minimum_confidence: float = 0.60
    minimum_agreement: float = 0.60
    minimum_data_completeness: float = 0.75
    minimum_depth: float = 1.0
    maximum_spread: float = 0.12
    fee_rate: float = 0.0
    fee_per_contract: float = 0.0
    slippage_bps: float = 0.0
    slippage_per_contract: float = 0.0

    @property
    def effective_minimum_edge(self) -> Decimal:
        """The configured threshold can tighten, but never weaken, 20 points."""
        return max(ABSOLUTE_MINIMUM_EDGE, Decimal(str(self.minimum_edge)))


def _direction_for_side(side: ContractSide | None) -> Direction:
    if side is ContractSide.YES:
        return Direction.UP
    if side is ContractSide.NO:
        return Direction.DOWN
    return Direction.FLAT


def _failure(gate: str, reason: str, observed: object, required: object) -> GateFailure:
    return GateFailure(gate=gate, reason=reason, observed=observed, required=required)


class DecisionEngine:
    def __init__(self, config: DecisionConfig | None = None) -> None:
        self.config = config or DecisionConfig()

    def _common_gates(
        self,
        market: MarketSnapshot,
        forecast: ProbabilityEstimate,
        features: FeatureSnapshot,
        benchmark: BenchmarkQuote,
        now: datetime,
        *,
        risk_locked: bool,
        duplicate_entry: bool,
    ) -> list[GateFailure]:
        cfg = self.config
        failures: list[GateFailure] = []
        if not market.valid or market.rejection_reasons:
            failures.append(
                _failure(
                    "market_validity",
                    "market discovery validation failed",
                    market.rejection_reasons,
                    "valid market",
                )
            )
        if market.status.lower() not in {"open", "active"}:
            failures.append(_failure("market_status", "market is not open", market.status, "open/active"))
        seconds = (market.expiration - now).total_seconds()
        if seconds < cfg.minimum_seconds_remaining or seconds > cfg.maximum_seconds_remaining:
            failures.append(
                _failure(
                    "time_window",
                    "contract is outside the safe entry window",
                    seconds,
                    (cfg.minimum_seconds_remaining, cfg.maximum_seconds_remaining),
                )
            )
        source = benchmark.source.lower()
        is_brti = "brti" in source or "bitcoin real time index" in source
        if not benchmark.primary or not is_brti:
            failures.append(
                _failure(
                    "primary_brti",
                    "forecast input is not explicitly primary BRTI",
                    (benchmark.primary, benchmark.source),
                    "primary CME CF BRTI",
                )
            )
        if not benchmark.is_live or benchmark.replay:
            failures.append(
                _failure(
                    "live_data",
                    "replay/non-live BRTI cannot authorize production entry",
                    (benchmark.is_live, benchmark.replay),
                    (True, False),
                )
            )
        benchmark_age = (now - benchmark.timestamp).total_seconds()
        if benchmark_age < -5 or benchmark_age > cfg.maximum_benchmark_age:
            failures.append(
                _failure(
                    "benchmark_freshness",
                    "primary benchmark is stale or future-dated",
                    benchmark_age,
                    cfg.maximum_benchmark_age,
                )
            )
        feature_age = (now - features.timestamp).total_seconds()
        if feature_age < -1 or feature_age > cfg.maximum_feature_age:
            failures.append(
                _failure(
                    "feature_freshness",
                    "features are stale or future-dated",
                    feature_age,
                    cfg.maximum_feature_age,
                )
            )
        if features.data_completeness < cfg.minimum_data_completeness:
            failures.append(
                _failure(
                    "data_completeness",
                    "insufficient causal BRTI history",
                    features.data_completeness,
                    cfg.minimum_data_completeness,
                )
            )
        if forecast.confidence < cfg.minimum_confidence:
            failures.append(
                _failure(
                    "confidence",
                    "ensemble confidence is below minimum",
                    forecast.confidence,
                    cfg.minimum_confidence,
                )
            )
        if forecast.signal_agreement < cfg.minimum_agreement:
            failures.append(
                _failure(
                    "agreement",
                    "ensemble components do not agree",
                    forecast.signal_agreement,
                    cfg.minimum_agreement,
                )
            )
        for side in (ContractSide.YES, ContractSide.NO):
            side_spread = spread(market.orderbook, side)
            if side_spread is None or side_spread > cfg.maximum_spread:
                failures.append(
                    _failure(
                        f"{side.value.lower()}_spread",
                        f"{side.value} spread is missing or too wide",
                        side_spread,
                        cfg.maximum_spread,
                    )
                )
            side_depth = depth(market.orderbook, side, asks=True)
            if side_depth < max(cfg.minimum_depth, cfg.quantity):
                failures.append(
                    _failure(
                        f"{side.value.lower()}_liquidity",
                        f"{side.value} executable depth is insufficient",
                        side_depth,
                        max(cfg.minimum_depth, cfg.quantity),
                    )
                )
        if risk_locked:
            failures.append(_failure("risk_lock", "risk controls are locked", True, False))
        if duplicate_entry:
            failures.append(_failure("duplicate", "entry duplicates a prior intent", True, False))
        active_orders = [
            order
            for order in market.open_orders
            if order.status.lower() in {"open", "pending", "resting"}
        ]
        if active_orders:
            failures.append(
                _failure(
                    "open_order",
                    "an order is already active for this market",
                    tuple(order.order_id for order in active_orders),
                    (),
                )
            )
        return failures

    @staticmethod
    def _can_exit(market: MarketSnapshot, side: ContractSide, quantity: float) -> bool:
        return depth(market.orderbook, side, asks=False) + 1e-12 >= quantity

    def decide(
        self,
        market: MarketSnapshot,
        forecast: ProbabilityEstimate,
        features: FeatureSnapshot,
        benchmark: BenchmarkQuote,
        *,
        now: datetime | None = None,
        risk_locked: bool = False,
        duplicate_entry: bool = False,
        quantity: float | None = None,
    ) -> DecisionResult:
        observed_now = utc_datetime(now or datetime.now(timezone.utc))
        cfg = self.config
        trade_quantity = quantity if quantity is not None else cfg.quantity
        if trade_quantity <= 0:
            raise ValueError("quantity must be positive")
        predicted_side = ContractSide.YES if forecast.p_up >= forecast.p_down else ContractSide.NO
        predicted_direction = _direction_for_side(predicted_side)
        position = market.current_position
        current_direction = _direction_for_side(position.side if position else None)
        failures = self._common_gates(
            market,
            forecast,
            features,
            benchmark,
            observed_now,
            risk_locked=risk_locked,
            duplicate_entry=duplicate_entry,
        )

        # A held thesis that reverses or loses trustworthy data exits to flat;
        # lack of executable bids forces HOLD rather than pretending an exit.
        if position is not None and position.quantity > 0:
            reliability_gates = {
                "market_validity",
                "market_status",
                "time_window",
                "primary_brti",
                "live_data",
                "benchmark_freshness",
                "feature_freshness",
                "data_completeness",
                "confidence",
                "agreement",
                "risk_lock",
            }
            thesis_reversed = position.side is not predicted_side
            unreliable = any(failure.gate in reliability_gates for failure in failures)
            if thesis_reversed or unreliable:
                reason = "forecast reversed the held thesis" if thesis_reversed else "held thesis lost reliable data"
                exit_quantity = min(position.quantity, trade_quantity)
                if self._can_exit(market, position.side, exit_quantity):
                    return DecisionResult(
                        action=DecisionAction.EXIT,
                        reason=f"{reason}; exit before any opposite entry",
                        gate_failures=tuple(failures),
                        current_direction=current_direction,
                        predicted_direction=predicted_direction,
                        trade_direction=Direction.FLAT,
                        selected_side=position.side,
                        predicted_probability=forecast.p_up if position.side is ContractSide.YES else forecast.p_down,
                        quantity=exit_quantity,
                        target_edge=cfg.target_edge,
                    )
                return DecisionResult(
                    action=DecisionAction.HOLD,
                    reason=f"{reason}, but exit liquidity is unavailable",
                    gate_failures=tuple(failures),
                    current_direction=current_direction,
                    predicted_direction=predicted_direction,
                    trade_direction=Direction.FLAT,
                    selected_side=position.side,
                    quantity=position.quantity,
                    target_edge=cfg.target_edge,
                )
            return DecisionResult(
                action=DecisionAction.HOLD,
                reason="existing same-side position; pyramiding is not allowed",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                selected_side=position.side,
                predicted_probability=forecast.p_up if position.side is ContractSide.YES else forecast.p_down,
                quantity=position.quantity,
                target_edge=cfg.target_edge,
            )

        executions = {}
        side_probabilities = {
            ContractSide.YES: forecast.p_up,
            ContractSide.NO: forecast.p_down,
        }
        for side in (ContractSide.YES, ContractSide.NO):
            try:
                executions[side] = estimate_buy_execution(
                    market.orderbook,
                    side,
                    trade_quantity,
                    fee_rate=cfg.fee_rate,
                    fee_per_contract=cfg.fee_per_contract,
                    slippage_bps=cfg.slippage_bps,
                    slippage_per_contract=cfg.slippage_per_contract,
                )
            except InsufficientDepthError as exc:
                failures.append(
                    _failure(
                        f"{side.value.lower()}_execution",
                        str(exc),
                        depth(market.orderbook, side, asks=True),
                        trade_quantity,
                    )
                )

        if not executions:
            return DecisionResult(
                action=DecisionAction.NO_TRADE,
                reason="neither side has executable depth",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                target_edge=cfg.target_edge,
            )
        edges = {
            side: side_probabilities[side] - execution.executable_cost
            for side, execution in executions.items()
        }
        selected_side = max(edges, key=lambda side: (edges[side], side.value))
        selected_execution = executions[selected_side]
        selected_edge = edges[selected_side]
        edge_decimal = Decimal(str(side_probabilities[selected_side])) - Decimal(
            str(selected_execution.executable_cost)
        )
        if edge_decimal + EDGE_TOLERANCE < cfg.effective_minimum_edge:
            failures.append(
                _failure(
                    "minimum_edge",
                    "best all-in edge is below the hard minimum",
                    selected_edge,
                    float(cfg.effective_minimum_edge),
                )
            )

        if failures:
            return DecisionResult(
                action=DecisionAction.NO_TRADE,
                reason="entry blocked by safety gates",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                selected_side=selected_side,
                predicted_probability=side_probabilities[selected_side],
                executable_cost=selected_execution.executable_cost,
                edge=selected_edge,
                target_edge=cfg.target_edge,
                quantity=trade_quantity,
                execution=selected_execution,
            )
        action = (
            DecisionAction.BUY_UP
            if selected_side is ContractSide.YES
            else DecisionAction.BUY_DOWN
        )
        target_text = (
            "meets target edge"
            if selected_edge + float(EDGE_TOLERANCE) >= cfg.target_edge
            else "meets hard minimum edge but is below target"
        )
        return DecisionResult(
            action=action,
            reason=f"{selected_side.value} {target_text}",
            gate_failures=(),
            current_direction=current_direction,
            predicted_direction=predicted_direction,
            trade_direction=_direction_for_side(selected_side),
            selected_side=selected_side,
            predicted_probability=side_probabilities[selected_side],
            executable_cost=selected_execution.executable_cost,
            edge=selected_edge,
            target_edge=cfg.target_edge,
            quantity=trade_quantity,
            execution=selected_execution,
        )


def make_decision(
    market: MarketSnapshot,
    forecast: ProbabilityEstimate,
    features: FeatureSnapshot,
    benchmark: BenchmarkQuote,
    **kwargs: object,
) -> DecisionResult:
    return DecisionEngine().decide(market, forecast, features, benchmark, **kwargs)
