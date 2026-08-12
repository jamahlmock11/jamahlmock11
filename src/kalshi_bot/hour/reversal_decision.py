"""Reversal-only entry decisions for the 1-hour Kalshi bot."""

from __future__ import annotations

from datetime import datetime, timezone

from kalshi_bot.config import AppConfig, HourReversalConfig
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
    SupportingAggregate,
    utc_datetime,
)
from kalshi_bot.execution.position_reversal import reversal_config_from_risk
from kalshi_bot.execution.stop_loss import evaluate_position_exit
from kalshi_bot.hour.reversal_engine import ReversalAssessment, ReversalTier, assess_reversal
from kalshi_bot.hour.reversal_state import ReversalStateTracker
from kalshi_bot.hour.trend_engine import TrendSnapshot
from kalshi_bot.hour.volatility_model import VolatilitySnapshot
from kalshi_bot.market.orderbook import depth, estimate_buy_execution, spread
from kalshi_bot.market.poll_alignment import PollSnapshot, market_poll_snapshot

if True:
    from kalshi_bot.execution.risk import RiskManager


def _failure(gate: str, reason: str, observed=None, required=None) -> GateFailure:
    return GateFailure(gate=gate, reason=reason, observed=observed, required=required)


def _direction_for_side(side: ContractSide | None) -> Direction:
    if side is ContractSide.YES:
        return Direction.UP
    if side is ContractSide.NO:
        return Direction.DOWN
    return Direction.FLAT


class HourReversalDecisionEngine:
    """Enter only when BTC momentum fails and Kalshi lags the repriced probability."""

    def __init__(
        self,
        config: AppConfig,
        *,
        state_tracker: ReversalStateTracker | None = None,
    ) -> None:
        self.config = config
        self.cfg: HourReversalConfig = config.hour_reversal
        self.state_tracker = state_tracker or ReversalStateTracker()

    def decide(
        self,
        market: MarketSnapshot,
        forecast: ProbabilityEstimate,
        features: FeatureSnapshot,
        benchmark: BenchmarkQuote,
        *,
        trend: TrendSnapshot,
        vol: VolatilitySnapshot,
        poll: PollSnapshot | None = None,
        supporting: SupportingAggregate | None = None,
        reversal: ReversalAssessment | None = None,
        now: datetime | None = None,
        risk_locked: bool = False,
        duplicate_entry: bool = False,
        risk_manager: RiskManager | None = None,
    ) -> DecisionResult:
        observed_now = utc_datetime(now or datetime.now(timezone.utc))
        poll = poll or market_poll_snapshot(market.orderbook)
        hour_cfg = self.config.hour
        trade_quantity = hour_cfg.order_quantity
        predicted_side = ContractSide.YES if forecast.p_up >= forecast.p_down else ContractSide.NO
        predicted_direction = _direction_for_side(predicted_side)
        position = market.current_position
        current_direction = _direction_for_side(position.side if position else None)
        failures: list[GateFailure] = []

        if risk_locked:
            failures.append(_failure("risk_lock", "risk lock active"))
        if duplicate_entry:
            failures.append(_failure("duplicate", "duplicate order intent"))
        if not market.valid:
            failures.append(_failure("market_validity", "market failed validation"))
        if market.status.lower() not in {"open", "active"}:
            failures.append(_failure("market_status", "market is not open"))
        if features.seconds_remaining + 1e-9 < self.cfg.min_seconds_remaining:
            failures.append(
                _failure(
                    "time_window",
                    "inside minimum entry window",
                    features.seconds_remaining,
                    self.cfg.min_seconds_remaining,
                )
            )
        if features.seconds_remaining > self.cfg.max_entry_seconds_remaining + 1e-9:
            failures.append(
                _failure(
                    "time_window",
                    "outside maximum entry window",
                    features.seconds_remaining,
                    self.cfg.max_entry_seconds_remaining,
                )
            )
        if features.data_completeness + 1e-12 < self.cfg.min_data_completeness:
            failures.append(
                _failure(
                    "data_completeness",
                    "insufficient causal BRTI history",
                    features.data_completeness,
                    self.cfg.min_data_completeness,
                )
            )
        if benchmark.is_proxy and self.config.data.benchmark_mode != "constituent_proxy":
            failures.append(_failure("primary_brti", "proxy benchmark cannot authorize live entry"))

        if position is not None and position.quantity > 0:
            exit_signal = evaluate_position_exit(
                market=market,
                position=position,
                forecast=forecast,
                features=features,
                failures=failures,
                predicted_side=predicted_side,
                quantity=trade_quantity,
                stop_loss_fraction=self.config.risk.stop_loss_fraction,
                opposite_edge_shift=self.config.risk.opposite_edge_shift,
                thesis_reversal_margin=self.config.risk.thesis_reversal_margin,
                thesis_reversal_enabled=self.config.risk.thesis_reversal_enabled,
                opposite_edge_exit_enabled=self.config.risk.opposite_edge_exit_enabled,
                recovery_hold_enabled=self.config.risk.recovery_hold_enabled,
                recovery_hold_min_probability=self.config.risk.recovery_hold_min_probability,
                recovery_hold_min_confidence=self.config.risk.recovery_hold_min_confidence,
                recovery_hold_min_agreement=self.config.risk.recovery_hold_min_agreement,
                min_hold_seconds=self.config.risk.min_hold_seconds,
                position_reversal=reversal_config_from_risk(self.config.risk),
                now=observed_now,
            )
            if exit_signal is not None:
                exit_quantity = min(position.quantity, trade_quantity)
                return DecisionResult(
                    action=DecisionAction.EXIT,
                    reason=exit_signal.reason,
                    gate_failures=tuple(failures),
                    current_direction=current_direction,
                    predicted_direction=predicted_direction,
                    trade_direction=Direction.FLAT,
                    selected_side=position.side,
                    predicted_probability=(
                        forecast.p_up if position.side is ContractSide.YES else forecast.p_down
                    ),
                    quantity=exit_quantity,
                    target_edge=self.cfg.min_entry_edge,
                    required_edge=self.cfg.min_entry_edge,
                    entry_strategy="reversal_exit",
                )
            return DecisionResult(
                action=DecisionAction.HOLD,
                reason="holding open reversal position",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                selected_side=position.side,
                quantity=position.quantity,
                target_edge=self.cfg.min_entry_edge,
                required_edge=self.cfg.min_entry_edge,
                entry_strategy="reversal",
            )

        if reversal is None:
            failures.append(_failure("reversal_score", "reversal assessment unavailable"))
            return DecisionResult(
                action=DecisionAction.NO_TRADE,
                reason="reversal assessment unavailable",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                target_edge=self.cfg.min_entry_edge,
                required_edge=self.cfg.min_entry_edge,
                entry_strategy="reversal",
            )

        if reversal.tier is ReversalTier.NONE:
            failures.append(
                _failure(
                    "reversal_score",
                    "reversal score below watch threshold",
                    reversal.score,
                    self.cfg.watch_score,
                )
            )
        elif reversal.tier is ReversalTier.WATCH:
            failures.append(
                _failure(
                    "reversal_score",
                    "reversal watch only; score below candidate threshold",
                    reversal.score,
                    self.cfg.min_reversal_score,
                )
            )

        if not reversal.confirmed:
            failures.append(
                _failure(
                    "reversal_confirmation",
                    reversal.confirmation_reason,
                    reversal.score,
                    self.cfg.min_reversal_score,
                )
            )

        if self.cfg.require_cross_feed_confirmation:
            if reversal.components.cross_exchange_confirmation + 1e-12 < self.cfg.min_cross_feed_score:
                failures.append(
                    _failure(
                        "cross_feed",
                        "multiple BTC feeds have not confirmed the reversal",
                        reversal.components.cross_exchange_confirmation,
                        self.cfg.min_cross_feed_score,
                    )
                )

        if reversal.reversal_direction is None:
            failures.append(_failure("reversal_direction", "no reversal direction established"))

        reversal_side = (
            ContractSide.NO
            if reversal.reversal_direction is Direction.DOWN
            else ContractSide.YES
            if reversal.reversal_direction is Direction.UP
            else None
        )
        if reversal_side is None:
            failures.append(_failure("reversal_direction", "reversal side unavailable"))

        yes_spread = spread(market.orderbook, ContractSide.YES) or 1.0
        no_spread = spread(market.orderbook, ContractSide.NO) or 1.0
        if max(yes_spread, no_spread) > self.cfg.max_spread + 1e-12:
            failures.append(
                _failure(
                    "spread",
                    "bid-ask spread too wide",
                    max(yes_spread, no_spread),
                    self.cfg.max_spread,
                )
            )

        execution = None
        selected_edge = None
        selected_prob = None
        if reversal_side is not None:
            side_depth = depth(market.orderbook, reversal_side, asks=True)
            if side_depth + 1e-12 < trade_quantity:
                failures.append(
                    _failure(
                        "liquidity",
                        "insufficient ask depth on reversal side",
                        side_depth,
                        trade_quantity,
                    )
                )
            else:
                try:
                    execution = estimate_buy_execution(
                        market.orderbook,
                        reversal_side,
                        trade_quantity,
                        fee_rate=self.config.execution.fee_rate,
                        fee_per_contract=self.config.execution.fee_per_contract,
                        slippage_bps=self.config.execution.slippage_bps,
                        slippage_per_contract=self.config.execution.slippage_per_contract,
                    )
                except Exception as exc:
                    failures.append(
                        _failure(
                            "liquidity",
                            "cannot estimate reversal-side execution",
                            str(exc),
                            trade_quantity,
                        )
                    )
                if execution is not None:
                    selected_prob = (
                        forecast.p_down if reversal_side is ContractSide.NO else forecast.p_up
                    )
                    selected_edge = selected_prob - execution.executable_cost
                    if selected_prob + 1e-12 < self.cfg.min_reversal_side_probability:
                        failures.append(
                            _failure(
                                "reversal_probability",
                                "calibrated reversal probability too low",
                                selected_prob,
                                self.cfg.min_reversal_side_probability,
                            )
                        )
                    if selected_edge + 1e-12 < self.cfg.min_entry_edge:
                        failures.append(
                            _failure(
                                "minimum_edge",
                                "reversal net edge below threshold after costs",
                                selected_edge,
                                self.cfg.min_entry_edge,
                            )
                        )

        if failures:
            return DecisionResult(
                action=DecisionAction.NO_TRADE,
                reason=f"reversal blocked: {reversal.summary}",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                selected_side=reversal_side,
                predicted_probability=selected_prob,
                executable_cost=execution.executable_cost if execution else None,
                edge=selected_edge,
                target_edge=self.cfg.min_entry_edge,
                required_edge=self.cfg.min_entry_edge,
                quantity=trade_quantity,
                execution=execution,
                entry_strategy="reversal",
            )

        action = (
            DecisionAction.BUY_DOWN
            if reversal_side is ContractSide.NO
            else DecisionAction.BUY_UP
        )
        return DecisionResult(
            action=action,
            reason=(
                f"reversal entry {reversal.tier.value} {reversal.score:.0f}/100: "
                f"{reversal.summary}; edge {selected_edge:.1%}"
            ),
            gate_failures=(),
            current_direction=current_direction,
            predicted_direction=predicted_direction,
            trade_direction=_direction_for_side(reversal_side),
            selected_side=reversal_side,
            predicted_probability=selected_prob,
            executable_cost=execution.executable_cost if execution else None,
            edge=selected_edge,
            target_edge=self.cfg.min_entry_edge,
            required_edge=self.cfg.min_entry_edge,
            quantity=trade_quantity,
            execution=execution,
            entry_strategy="reversal",
        )
