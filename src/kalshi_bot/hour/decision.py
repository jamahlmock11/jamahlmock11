"""Safety-gated 1-hour decisions with dynamic edge and trade tiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from kalshi_bot.strategies.decision import _direction_for_side, _failure
from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    DecisionResult,
    Direction,
    EntryTiming,
    FeatureSnapshot,
    GateFailure,
    MarketSnapshot,
    ProbabilityEstimate,
    TradeTier,
    utc_datetime,
)
from kalshi_bot.hour.edge_engine import assess_edge, EdgeAssessment
from kalshi_bot.hour.trend_engine import TrendSnapshot
from kalshi_bot.hour.volatility_model import VolatilitySnapshot
from kalshi_bot.execution.position_reversal import PositionReversalConfig, reversal_config_from_risk
from kalshi_bot.execution.stop_loss import evaluate_position_exit
from kalshi_bot.market.orderbook import (
    InsufficientDepthError,
    depth,
    estimate_buy_execution,
    spread,
)
from kalshi_bot.strategies.longshot import (
    evaluate_longshot_exit,
    longshot_exit_config,
    resolve_longshot_entries,
)
from kalshi_bot.market.poll_alignment import (
    PollConfig as PollAlignmentConfig,
    evaluate_poll_gate,
    market_poll_snapshot,
    poll_gate_config_from_model,
)
from kalshi_bot.config import HourEdgeConfig, HourStrategyConfig, LongshotConfig, PollConfig, CertaintyHoldConfig


@dataclass(frozen=True)
class HourDecisionConfig:
    hour: HourStrategyConfig
    edge: HourEdgeConfig
    poll: PollConfig = field(default_factory=PollConfig)
    longshot: LongshotConfig = field(default_factory=LongshotConfig)
    maximum_benchmark_age: float = 20.0
    maximum_feature_age: float = 15.0
    fee_rate: float = 0.0
    fee_per_contract: float = 0.0
    slippage_bps: float = 5.0
    slippage_per_contract: float = 0.0
    allow_replay_data: bool = False
    allow_proxy_data: bool = False
    proxy_minimum_constituents: int = 3
    proxy_maximum_dispersion: float = 0.003
    proxy_entry_cutoff_seconds: float = 300.0
    stop_loss_fraction: float = 0.45
    opposite_edge_shift: float = 0.15
    thesis_reversal_margin: float = 0.10
    thesis_reversal_enabled: bool = False
    opposite_edge_exit_enabled: bool = False
    recovery_hold_enabled: bool = False
    recovery_hold_min_probability: float = 0.58
    recovery_hold_min_confidence: float = 0.58
    recovery_hold_min_agreement: float = 0.58
    min_hold_seconds: float = 0.0
    position_reversal: PositionReversalConfig = field(default_factory=PositionReversalConfig)
    certainty_hold: CertaintyHoldConfig = field(default_factory=CertaintyHoldConfig)


class HourDecisionEngine:
    def __init__(self, config: HourDecisionConfig | None = None) -> None:
        self.config = config or HourDecisionConfig(
            hour=HourStrategyConfig(),
            edge=HourEdgeConfig(),
        )

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
        hour = cfg.hour
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
        if seconds < hour.min_seconds_remaining or seconds > hour.max_entry_seconds_remaining:
            failures.append(
                _failure(
                    "time_window",
                    "contract is outside the safe entry window",
                    seconds,
                    (hour.min_seconds_remaining, hour.max_entry_seconds_remaining),
                )
            )
        source = benchmark.source.lower()
        is_brti = "brti" in source or "bitcoin real time index" in source
        official_brti = benchmark.primary and not benchmark.is_proxy and is_brti
        permitted_proxy = benchmark.is_proxy and cfg.allow_proxy_data and is_brti
        if not official_brti and not permitted_proxy:
            failures.append(
                _failure(
                    "primary_brti",
                    "forecast input is neither official BRTI nor an allowed PAPER proxy",
                    (benchmark.primary, benchmark.is_proxy, benchmark.source),
                    "official CME CF BRTI",
                )
            )
        if benchmark.is_proxy:
            if benchmark.constituent_count < cfg.proxy_minimum_constituents:
                failures.append(
                    _failure(
                        "proxy_constituents",
                        "too few healthy constituent venues for proxy use",
                        benchmark.constituent_count,
                        cfg.proxy_minimum_constituents,
                    )
                )
            if benchmark.dispersion > cfg.proxy_maximum_dispersion:
                failures.append(
                    _failure(
                        "proxy_dispersion",
                        "constituent proxy dispersion is too high",
                        benchmark.dispersion,
                        cfg.proxy_maximum_dispersion,
                    )
                )
            if seconds <= cfg.proxy_entry_cutoff_seconds:
                failures.append(
                    _failure(
                        "proxy_late_contract",
                        "proxy entries are disabled near settlement",
                        seconds,
                        f">{cfg.proxy_entry_cutoff_seconds}",
                    )
                )
        if (not benchmark.is_live or benchmark.replay) and not cfg.allow_replay_data:
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
        if features.data_completeness < hour.min_data_completeness:
            failures.append(
                _failure(
                    "data_completeness",
                    "insufficient causal BRTI history",
                    features.data_completeness,
                    hour.min_data_completeness,
                )
            )
        required_confidence = hour.min_confidence + (
            0.08 if benchmark.is_proxy else 0.0
        )
        if forecast.confidence < required_confidence:
            failures.append(
                _failure(
                    "confidence",
                    "model confidence is below minimum",
                    forecast.confidence,
                    required_confidence,
                )
            )
        if forecast.signal_agreement < hour.min_signal_agreement:
            failures.append(
                _failure(
                    "agreement",
                    "signals do not agree sufficiently",
                    forecast.signal_agreement,
                    hour.min_signal_agreement,
                )
            )
        for side in (ContractSide.YES, ContractSide.NO):
            side_spread = spread(market.orderbook, side)
            if side_spread is None or side_spread > hour.max_spread:
                failures.append(
                    _failure(
                        f"{side.value.lower()}_spread",
                        f"{side.value} spread is missing or too wide",
                        side_spread,
                        hour.max_spread,
                    )
                )
            side_depth = depth(market.orderbook, side, asks=True)
            if side_depth < max(hour.order_quantity, 1.0):
                failures.append(
                    _failure(
                        f"{side.value.lower()}_liquidity",
                        f"{side.value} executable depth is insufficient",
                        side_depth,
                        hour.order_quantity,
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
        trend: TrendSnapshot,
        vol: VolatilitySnapshot,
        regime,
        model_stability: float,
        *,
        now: datetime | None = None,
        risk_locked: bool = False,
        duplicate_entry: bool = False,
        quantity: float | None = None,
    ) -> DecisionResult:
        observed_now = utc_datetime(now or datetime.now(timezone.utc))
        cfg = self.config
        hour = cfg.hour
        edge_cfg = cfg.edge
        trade_quantity = quantity if quantity is not None else hour.order_quantity
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

        if position is not None and position.quantity > 0:
            held_prob = forecast.p_up if position.side is ContractSide.YES else forecast.p_down
            if cfg.longshot.enabled:
                longshot_exit = evaluate_longshot_exit(
                    market=market,
                    position=position,
                    quantity=trade_quantity,
                    cfg=longshot_exit_config(cfg.longshot),
                    now=observed_now,
                )
                if longshot_exit is not None:
                    exit_quantity = min(position.quantity, trade_quantity)
                    if self._can_exit(market, position.side, exit_quantity):
                        return DecisionResult(
                            action=DecisionAction.EXIT,
                            reason=longshot_exit.reason,
                            gate_failures=tuple(failures),
                            current_direction=current_direction,
                            predicted_direction=predicted_direction,
                            trade_direction=Direction.FLAT,
                            selected_side=position.side,
                            predicted_probability=held_prob,
                            quantity=exit_quantity,
                            target_edge=edge_cfg.preferred_edge,
                        )
            held_prob = forecast.p_up if position.side is ContractSide.YES else forecast.p_down
            exit_signal = evaluate_position_exit(
                market=market,
                position=position,
                forecast=forecast,
                features=features,
                failures=failures,
                predicted_side=predicted_side,
                quantity=trade_quantity,
                stop_loss_fraction=0.0 if cfg.longshot.enabled else cfg.stop_loss_fraction,
                opposite_edge_shift=cfg.opposite_edge_shift,
                thesis_reversal_margin=cfg.thesis_reversal_margin,
                thesis_reversal_enabled=cfg.thesis_reversal_enabled,
                opposite_edge_exit_enabled=cfg.opposite_edge_exit_enabled,
                recovery_hold_enabled=cfg.recovery_hold_enabled,
                recovery_hold_min_probability=cfg.recovery_hold_min_probability,
                recovery_hold_min_confidence=cfg.recovery_hold_min_confidence,
                recovery_hold_min_agreement=cfg.recovery_hold_min_agreement,
                min_hold_seconds=cfg.min_hold_seconds,
                position_reversal=cfg.position_reversal,
                certainty_hold=cfg.certainty_hold,
                now=observed_now,
            )
            if exit_signal is not None:
                exit_quantity = min(position.quantity, trade_quantity)
                if self._can_exit(market, position.side, exit_quantity):
                    return DecisionResult(
                        action=DecisionAction.EXIT,
                        reason=exit_signal.reason,
                        gate_failures=tuple(failures),
                        current_direction=current_direction,
                        predicted_direction=predicted_direction,
                        trade_direction=Direction.FLAT,
                        selected_side=position.side,
                        predicted_probability=held_prob,
                        quantity=exit_quantity,
                        target_edge=edge_cfg.preferred_edge,
                    )
                return DecisionResult(
                    action=DecisionAction.HOLD,
                    reason=f"{exit_signal.reason}, but exit liquidity is unavailable",
                    gate_failures=tuple(failures),
                    current_direction=current_direction,
                    predicted_direction=predicted_direction,
                    trade_direction=Direction.FLAT,
                    selected_side=position.side,
                    quantity=position.quantity,
                    target_edge=edge_cfg.preferred_edge,
                )
            return DecisionResult(
                action=DecisionAction.HOLD,
                reason="thesis remains valid; holding to expiration or profit target",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                selected_side=position.side,
                predicted_probability=held_prob,
                quantity=position.quantity,
                target_edge=edge_cfg.preferred_edge,
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

        entry_ctx = None
        if cfg.longshot.enabled:
            poll_cfg = poll_gate_config_from_model(cfg.poll)
            entry_ctx = resolve_longshot_entries(
                executions,
                poll=market_poll_snapshot(market.orderbook),
                forecast=forecast,
                seconds_remaining=features.seconds_remaining,
                cfg=cfg.longshot,
                poll_cfg=poll_cfg,
                features=features,
            )
            failures.extend(entry_ctx.failures)
            executions = entry_ctx.executions

        if not executions:
            return DecisionResult(
                action=DecisionAction.NO_TRADE,
                reason="neither side has executable depth",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                target_edge=edge_cfg.preferred_edge,
            )

        yes_spread = spread(market.orderbook, ContractSide.YES) or 1.0
        no_spread = spread(market.orderbook, ContractSide.NO) or 1.0
        yes_depth = depth(market.orderbook, ContractSide.YES, asks=True)
        no_depth = depth(market.orderbook, ContractSide.NO, asks=True)

        edge_assessment = assess_edge(
            up_probability=forecast.p_up,
            down_probability=forecast.p_down,
            up_executable=executions.get(ContractSide.YES).executable_cost if ContractSide.YES in executions else None,
            down_executable=executions.get(ContractSide.NO).executable_cost if ContractSide.NO in executions else None,
            seconds_remaining=features.seconds_remaining,
            volatility=vol,
            yes_spread=yes_spread,
            no_spread=no_spread,
            yes_depth=yes_depth,
            no_depth=no_depth,
            confidence=forecast.confidence,
            agreement=forecast.signal_agreement,
            regime=regime,
            z_distance=features.z_distance_to_strike,
            trend=trend,
            model_stability=model_stability,
            hour_cfg=hour,
            edge_cfg=edge_cfg,
            is_proxy=benchmark.is_proxy,
        )

        edges = {
            ContractSide.YES: edge_assessment.up_edge,
            ContractSide.NO: edge_assessment.down_edge,
        }
        valid_edges = {k: v for k, v in edges.items() if v is not None}
        if not valid_edges:
            failures.append(_failure("edge", "no executable edge available", None, "positive edge"))
            return DecisionResult(
                action=DecisionAction.NO_TRADE,
                reason="no executable edge",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                target_edge=edge_cfg.preferred_edge,
                required_edge=edge_assessment.required_edge,
                trade_tier=TradeTier.NONE,
                entry_timing=edge_assessment.entry_timing,
            )

        selected_side = max(valid_edges, key=lambda side: (valid_edges[side], side.value))
        if entry_ctx is not None and entry_ctx.forced_side is not None:
            if entry_ctx.forced_side in valid_edges:
                selected_side = entry_ctx.forced_side
            else:
                failures.append(
                    _failure(
                        "extreme_poll_favorite",
                        "extreme poll favorite is not executable",
                        entry_ctx.forced_side.value,
                        entry_ctx.max_entry_price,
                    )
                )
        selected_edge = valid_edges.get(selected_side)
        if selected_edge is None:
            selected_side = max(valid_edges, key=lambda side: (valid_edges[side], side.value))
            selected_edge = valid_edges[selected_side]
        selected_execution = executions[selected_side]
        required = (
            entry_ctx.min_edge_override
            if entry_ctx is not None and entry_ctx.min_edge_override is not None
            else cfg.longshot.min_edge if cfg.longshot.enabled else edge_assessment.required_edge
        )
        if not cfg.longshot.enabled:
            required = edge_assessment.required_edge

        dominant_side = (
            ContractSide.YES if forecast.p_up >= forecast.p_down else ContractSide.NO
        )
        dominant_prob = max(forecast.p_up, forecast.p_down)
        alignment_required = (
            cfg.longshot.require_forecast_alignment
            if cfg.longshot.enabled
            else hour.require_forecast_alignment
        )
        if (
            alignment_required
            and dominant_prob + 1e-12 >= hour.forecast_alignment_min_probability
            and selected_side is not dominant_side
        ):
            failures.append(
                _failure(
                    "forecast_alignment",
                    "best edge side conflicts with dominant forecast direction",
                    selected_side.value,
                    dominant_side.value,
                )
            )

        if cfg.longshot.enabled:
            if selected_edge is None or (
                entry_ctx is None
                or entry_ctx.min_edge_override is None
                or entry_ctx.min_edge_override >= 0
            ):
                if selected_edge is None or selected_edge + 1e-12 < required:
                    failures.append(
                        _failure(
                            "minimum_edge",
                            "longshot edge below required threshold",
                            selected_edge,
                            required,
                        )
                    )
        elif edge_assessment.trade_tier is TradeTier.NONE:
            failures.append(
                _failure(
                    "minimum_edge",
                    "edge below required threshold or tier requirements not met",
                    selected_edge,
                    required,
                )
            )

        poll_active = (not cfg.longshot.enabled) or cfg.longshot.poll_enabled
        bypass_poll = (
            entry_ctx is not None and entry_ctx.extreme_poll_active
        )
        if poll_active and not bypass_poll:
            poll_cfg = poll_gate_config_from_model(cfg.poll)
            if cfg.longshot.enabled and poll_cfg.mode == "legacy":
                poll_cfg = PollAlignmentConfig(
                    mode="confirm_aligned",
                    confirm_threshold=poll_cfg.confirm_threshold,
                    favorable_min=poll_cfg.favorable_min,
                    favorable_max=poll_cfg.favorable_max,
                    low_poll_threshold=poll_cfg.low_poll_threshold,
                    counter_evidence_min_probability=poll_cfg.counter_evidence_min_probability,
                    counter_evidence_min_confidence=poll_cfg.counter_evidence_min_confidence,
                    counter_evidence_min_agreement=poll_cfg.counter_evidence_min_agreement,
                    low_poll_min_probability=poll_cfg.low_poll_min_probability,
                    low_poll_min_confidence=poll_cfg.low_poll_min_confidence,
                    low_poll_min_agreement=poll_cfg.low_poll_min_agreement,
                )
            poll_failure = evaluate_poll_gate(
                selected_side=selected_side,
                forecast=forecast,
                poll=market_poll_snapshot(market.orderbook),
                cfg=poll_cfg,
            )
            if poll_failure is not None:
                failures.append(poll_failure)

        if failures:
            return DecisionResult(
                action=DecisionAction.NO_TRADE,
                reason="entry blocked by safety gates or insufficient edge",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                selected_side=selected_side,
                predicted_probability=side_probabilities[selected_side],
                executable_cost=selected_execution.executable_cost,
                edge=selected_edge,
                target_edge=edge_cfg.preferred_edge,
                required_edge=required,
                trade_tier=edge_assessment.trade_tier,
                entry_timing=edge_assessment.entry_timing,
                quantity=trade_quantity,
                execution=selected_execution,
            )

        action = (
            DecisionAction.BUY_UP
            if selected_side is ContractSide.YES
            else DecisionAction.BUY_DOWN
        )
        tier_label = edge_assessment.trade_tier.value if not cfg.longshot.enabled else "longshot"
        size_multiplier = (
            edge_assessment.size_multiplier * cfg.longshot.position_size_mult
            if cfg.longshot.enabled
            else edge_assessment.size_multiplier
        )
        return DecisionResult(
            action=action,
            reason=f"{tier_label} trade: {selected_side.value} edge {selected_edge:.1%} >= required {required:.1%}",
            gate_failures=(),
            current_direction=current_direction,
            predicted_direction=predicted_direction,
            trade_direction=_direction_for_side(selected_side),
            selected_side=selected_side,
            predicted_probability=side_probabilities[selected_side],
            executable_cost=selected_execution.executable_cost,
            edge=selected_edge,
            target_edge=edge_cfg.preferred_edge,
            required_edge=required,
            trade_tier=edge_assessment.trade_tier,
            entry_timing=edge_assessment.entry_timing,
            size_multiplier=size_multiplier,
            quantity=trade_quantity,
            execution=selected_execution,
        )
