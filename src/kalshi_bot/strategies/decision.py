"""Safety-gated trading decisions using executable, all-in contract costs."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from kalshi_bot.config import AppConfig, LongshotConfig, PollConfig, CertaintyHoldConfig
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
from kalshi_bot.execution.position_reversal import (
    PositionReversalConfig,
    reversal_config_from_risk,
)
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
from kalshi_bot.strategies.entry_filters import is_in_chop_zone
from kalshi_bot.market.poll_alignment import (
    PollConfig as PollAlignmentConfig,
    PollSnapshot,
    evaluate_poll_gate,
    market_poll_snapshot,
    poll_gate_config_from_model,
)

if TYPE_CHECKING:
    from kalshi_bot.execution.risk import RiskManager

ABSOLUTE_MINIMUM_EDGE = Decimal("0.10")
EDGE_TOLERANCE = Decimal("0.000000000001")
DEFAULT_MINIMUM_EDGE = float(ABSOLUTE_MINIMUM_EDGE)


def edge_gap_details(decision: DecisionResult | None) -> dict[str, float | None]:
    """Return observed, required, and shortfall edge in Kalshi cents (pp)."""
    if decision is None:
        return {"observed_cents": None, "required_cents": None, "gap_cents": None}

    observed = decision.edge
    required = decision.required_edge
    for failure in decision.gate_failures:
        if failure.gate != "minimum_edge":
            continue
        if failure.observed is not None:
            observed = float(failure.observed)
        if failure.required is not None:
            required = float(failure.required)
        break

    if required is None:
        required = DEFAULT_MINIMUM_EDGE
    if observed is None:
        return {
            "observed_cents": None,
            "required_cents": required * 100.0,
            "gap_cents": None,
        }

    observed_cents = observed * 100.0
    required_cents = required * 100.0
    gap_cents = max(0.0, required_cents - observed_cents)
    return {
        "observed_cents": observed_cents,
        "required_cents": required_cents,
        "gap_cents": gap_cents,
    }


def format_edge_gap(decision: DecisionResult | None) -> str:
    """Human-readable Kalshi edge gap, e.g. 'Need 11¢ more (9¢ have · 20¢ need)'."""
    details = edge_gap_details(decision)
    observed = details["observed_cents"]
    required = details["required_cents"]
    gap = details["gap_cents"]
    if observed is None or required is None:
        return "Edge unavailable (no executable quote)"

    def cents(value: float) -> str:
        if abs(value) < 1.0:
            return f"{value:.1f}"
        return f"{value:.0f}"

    if gap is None or gap <= 0.05:
        surplus = observed - required
        if surplus > 0.05:
            return f"Met (+{cents(surplus)}¢ above {cents(required)}¢ minimum)"
        return f"Met ({cents(observed)}¢ have · {cents(required)}¢ need)"
    shortfall = math.ceil(gap - 1e-9)
    return f"Need {shortfall:.0f}¢ more ({cents(observed)}¢ have · {cents(required)}¢ need)"


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
    late_seconds: float = 120.0
    late_minimum_edge: float = 0.25
    final_seconds: float = 60.0
    final_minimum_edge: float = 0.25
    late_favorite_seconds: float = 420.0
    late_favorite_poll_threshold: float = 0.78
    late_favorite_min_edge: float = 0.04
    min_entry_executable_cost: float = 0.08
    minimum_dominant_poll: float | None = None
    require_dominant_poll_side: bool = False
    late_confidence_increment: float = 0.10
    allow_replay_data: bool = False
    allow_proxy_data: bool = False
    proxy_minimum_edge: float = 0.25
    proxy_confidence_increment: float = 0.10
    proxy_minimum_constituents: int = 3
    proxy_maximum_dispersion: float = 0.003
    proxy_entry_cutoff_seconds: float = 120.0
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
    poll: PollConfig = field(default_factory=PollConfig)
    longshot: LongshotConfig = field(default_factory=LongshotConfig)
    chop_zone_min_sigma: float = 0.0
    require_orderbook_depth: bool = False

    @property
    def effective_minimum_edge(self) -> Decimal:
        """Longshot mode allows 10¢ edges; otherwise the 15¢ floor applies."""
        if self.longshot.enabled:
            return Decimal(str(self.longshot.min_edge))
        return max(ABSOLUTE_MINIMUM_EDGE, Decimal(str(self.minimum_edge)))


def decision_config_from_app(
    config: AppConfig,
    *,
    maximum_seconds_remaining: float | None = None,
) -> DecisionConfig:
    """Build a DecisionConfig from application settings (shared by 15m and 1h bots)."""
    ls = config.longshot
    strategy = config.strategy
    entry_window = (
        ls.entry_window_seconds
        if ls.enabled
        else (maximum_seconds_remaining or strategy.max_entry_seconds_remaining)
    )
    return DecisionConfig(
        minimum_edge=ls.min_edge if ls.enabled else strategy.min_edge,
        target_edge=strategy.target_edge,
        quantity=strategy.order_quantity,
        maximum_benchmark_age=config.data.max_brti_age_seconds,
        minimum_seconds_remaining=strategy.min_seconds_remaining,
        maximum_seconds_remaining=entry_window,
        minimum_confidence=ls.min_confidence if ls.enabled else strategy.min_confidence,
        minimum_agreement=(
            ls.min_signal_agreement if ls.enabled else strategy.min_signal_agreement
        ),
        minimum_data_completeness=strategy.min_data_completeness,
        minimum_depth=strategy.order_quantity,
        maximum_spread=strategy.max_spread,
        fee_rate=config.execution.fee_rate,
        fee_per_contract=config.execution.fee_per_contract,
        slippage_bps=config.execution.slippage_bps,
        slippage_per_contract=config.execution.slippage_per_contract,
        late_seconds=strategy.late_seconds,
        late_minimum_edge=ls.min_edge if ls.enabled else strategy.target_edge,
        final_seconds=strategy.final_seconds,
        final_minimum_edge=ls.min_edge if ls.enabled else strategy.final_min_edge,
        late_confidence_increment=strategy.late_confidence_increment,
        late_favorite_seconds=strategy.late_favorite_seconds,
        late_favorite_poll_threshold=strategy.late_favorite_poll_threshold,
        late_favorite_min_edge=strategy.late_favorite_min_edge,
        min_entry_executable_cost=strategy.min_entry_executable_cost,
        minimum_dominant_poll=strategy.minimum_dominant_poll,
        require_dominant_poll_side=strategy.require_dominant_poll_side,
        allow_proxy_data=(
            config.execution.dry_run and config.data.benchmark_mode == "constituent_proxy"
        ),
        proxy_minimum_constituents=config.data.min_supporting_venues,
        proxy_maximum_dispersion=config.data.max_supporting_dispersion,
        stop_loss_fraction=config.risk.stop_loss_fraction,
        opposite_edge_shift=config.risk.opposite_edge_shift,
        thesis_reversal_margin=config.risk.thesis_reversal_margin,
        thesis_reversal_enabled=False if ls.enabled else config.risk.thesis_reversal_enabled,
        opposite_edge_exit_enabled=(
            False if ls.enabled else config.risk.opposite_edge_exit_enabled
        ),
        recovery_hold_enabled=False if ls.enabled else config.risk.recovery_hold_enabled,
        recovery_hold_min_probability=config.risk.recovery_hold_min_probability,
        recovery_hold_min_confidence=config.risk.recovery_hold_min_confidence,
        recovery_hold_min_agreement=config.risk.recovery_hold_min_agreement,
        min_hold_seconds=config.risk.min_hold_seconds,
        position_reversal=reversal_config_from_risk(config.risk),
        certainty_hold=config.risk.certainty_hold,
        poll=config.poll,
        longshot=config.longshot,
        chop_zone_min_sigma=strategy.chop_zone_min_sigma,
        require_orderbook_depth=strategy.require_orderbook_depth,
    )


def _direction_for_side(side: ContractSide | None) -> Direction:
    if side is ContractSide.YES:
        return Direction.UP
    if side is ContractSide.NO:
        return Direction.DOWN
    return Direction.FLAT


def _failure(gate: str, reason: str, observed: object, required: object) -> GateFailure:
    return GateFailure(gate=gate, reason=reason, observed=observed, required=required)


def _crowd_context_suffix(entry_ctx: object | None) -> str:
    if entry_ctx is None:
        return ""
    strike_hold = getattr(entry_ctx, "strike_hold", None)
    if strike_hold is None:
        return ""
    return f"; {strike_hold.summary}"


def _late_favorite_edge_floor(
    *,
    seconds_remaining: float,
    poll: PollSnapshot,
    selected_side: ContractSide,
    cfg: DecisionConfig,
) -> float | None:
    if cfg.late_favorite_seconds <= 0:
        return None
    if seconds_remaining > cfg.late_favorite_seconds:
        return None
    if poll.dominant_poll is None or poll.dominant_side is None:
        return None
    if poll.dominant_poll + 1e-12 < cfg.late_favorite_poll_threshold:
        return None
    if selected_side is not poll.dominant_side:
        return None
    return cfg.late_favorite_min_edge


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
            if seconds < cfg.minimum_seconds_remaining:
                failures.append(
                    _failure(
                        "last_minute",
                        "entries are blocked in the final minute before expiry",
                        seconds,
                        cfg.minimum_seconds_remaining,
                    )
                )
            else:
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
        if features.data_completeness < cfg.minimum_data_completeness:
            failures.append(
                _failure(
                    "data_completeness",
                    "insufficient causal BRTI history",
                    features.data_completeness,
                    cfg.minimum_data_completeness,
                )
            )
        if is_in_chop_zone(features, cfg.chop_zone_min_sigma):
            failures.append(
                _failure(
                    "chop_zone",
                    "spot is inside the strike dead zone (noise-dominated edge)",
                    abs(features.z_distance_to_strike),
                    cfg.chop_zone_min_sigma,
                )
            )
        required_confidence = cfg.minimum_confidence + (
            cfg.proxy_confidence_increment if benchmark.is_proxy else 0.0
        )
        required_confidence = min(1.0, required_confidence)
        if forecast.confidence < required_confidence:
            failures.append(
                _failure(
                    "confidence",
                    "ensemble confidence is below minimum",
                    forecast.confidence,
                    required_confidence,
                )
            )
        seconds = (market.expiration - now).total_seconds()
        late_confidence = max(
            required_confidence,
            min(1.0, cfg.minimum_confidence + cfg.late_confidence_increment),
        )
        if seconds <= cfg.late_seconds and forecast.confidence < late_confidence:
            if not cfg.longshot.enabled:
                failures.append(
                    _failure(
                        "late_confidence",
                        "late-contract confidence is below the conservative minimum",
                        forecast.confidence,
                        late_confidence,
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
            if cfg.require_orderbook_depth and side_depth < max(cfg.minimum_depth, cfg.quantity):
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
        risk_manager: RiskManager | None = None,
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

        # Stop-loss, thesis reversal, or data loss exits to flat before new entries.
        if position is not None and position.quantity > 0:
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
                            predicted_probability=forecast.p_up if position.side is ContractSide.YES else forecast.p_down,
                            quantity=exit_quantity,
                            target_edge=cfg.target_edge,
                        )
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
                        predicted_probability=forecast.p_up if position.side is ContractSide.YES else forecast.p_down,
                        quantity=exit_quantity,
                        target_edge=cfg.target_edge,
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

        entry_ctx = None
        if cfg.longshot.enabled or cfg.longshot.follow_extreme_poll:
            poll_cfg = poll_gate_config_from_model(cfg.poll)
            entry_ctx = resolve_longshot_entries(
                executions,
                poll=market_poll_snapshot(market.orderbook),
                forecast=forecast,
                seconds_remaining=(market.expiration - observed_now).total_seconds(),
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
                target_edge=cfg.target_edge,
            )
        edges = {
            side: side_probabilities[side] - execution.executable_cost
            for side, execution in executions.items()
        }
        if entry_ctx is not None and entry_ctx.forced_side is not None:
            selected_side = entry_ctx.forced_side
        else:
            selected_side = max(edges, key=lambda side: (edges[side], side.value))
        selected_execution = executions[selected_side]
        selected_edge = edges[selected_side]
        edge_decimal = Decimal(str(side_probabilities[selected_side])) - Decimal(
            str(selected_execution.executable_cost)
        )
        seconds_remaining = (market.expiration - observed_now).total_seconds()
        poll_snapshot = market_poll_snapshot(market.orderbook)
        if cfg.minimum_dominant_poll is not None:
            min_poll = cfg.minimum_dominant_poll
            if (
                poll_snapshot.dominant_poll is None
                or poll_snapshot.dominant_poll + 1e-12 < min_poll
            ):
                failures.append(
                    _failure(
                        "poll_favorite",
                        "market has no high-probability favorite at required poll level",
                        poll_snapshot.dominant_poll,
                        min_poll,
                    )
                )
            elif (
                cfg.require_dominant_poll_side
                and poll_snapshot.dominant_side is not None
                and selected_side is not poll_snapshot.dominant_side
            ):
                failures.append(
                    _failure(
                        "poll_favorite",
                        "entry must be on the market poll favorite side",
                        selected_side.value,
                        poll_snapshot.dominant_side.value,
                    )
                )
        required_edge = cfg.effective_minimum_edge
        if entry_ctx is not None and entry_ctx.min_edge_override is not None:
            required_edge = Decimal(str(entry_ctx.min_edge_override))
        if benchmark.is_proxy and not cfg.longshot.enabled:
            required_edge = max(required_edge, Decimal(str(cfg.proxy_minimum_edge)))
        late_favorite_edge = _late_favorite_edge_floor(
            seconds_remaining=seconds_remaining,
            poll=poll_snapshot,
            selected_side=selected_side,
            cfg=cfg,
        )
        if late_favorite_edge is not None:
            required_edge = Decimal(str(late_favorite_edge))
        elif not cfg.longshot.enabled:
            if seconds_remaining <= cfg.late_seconds:
                required_edge = max(required_edge, Decimal(str(cfg.late_minimum_edge)))
            if seconds_remaining <= cfg.final_seconds:
                required_edge = max(required_edge, Decimal(str(cfg.final_minimum_edge)))
        if entry_ctx is not None and entry_ctx.min_edge_override is not None:
            if entry_ctx.min_edge_override >= 0:
                required_edge = Decimal(str(entry_ctx.min_edge_override))
        if selected_execution.executable_cost + 1e-12 < cfg.min_entry_executable_cost:
            failures.append(
                _failure(
                    "min_entry_price",
                    "executable entry price is below the minimum for live entries",
                    selected_execution.executable_cost,
                    cfg.min_entry_executable_cost,
                )
            )
        if (
            entry_ctx is None
            or entry_ctx.min_edge_override is None
            or entry_ctx.min_edge_override >= 0
        ):
            if edge_decimal + EDGE_TOLERANCE < required_edge:
                failures.append(
                    _failure(
                        "minimum_edge",
                        "best all-in edge is below the applicable hard minimum",
                        selected_edge,
                        float(required_edge),
                    )
                )

        size_multiplier = (
            cfg.longshot.position_size_mult if cfg.longshot.enabled else 1.0
        )
        if risk_manager is not None and risk_manager.config.risk.kelly_enabled:
            kelly_qty = risk_manager.kelly_contracts_for_entry(
                edge=selected_edge,
                executable_cost=selected_execution.executable_cost,
                size_multiplier=size_multiplier,
                ticker=market.ticker,
                min_edge=float(required_edge),
            )
            if kelly_qty <= 0:
                failures.append(
                    _failure(
                        "kelly_sizing",
                        "Kelly sizing produced zero affordable contracts",
                        selected_edge,
                        float(required_edge),
                    )
                )
            elif kelly_qty != trade_quantity:
                try:
                    selected_execution = estimate_buy_execution(
                        market.orderbook,
                        selected_side,
                        kelly_qty,
                        fee_rate=cfg.fee_rate,
                        fee_per_contract=cfg.fee_per_contract,
                        slippage_bps=cfg.slippage_bps,
                        slippage_per_contract=cfg.slippage_per_contract,
                    )
                    trade_quantity = float(kelly_qty)
                    selected_edge = (
                        side_probabilities[selected_side]
                        - selected_execution.executable_cost
                    )
                    edge_decimal = Decimal(str(side_probabilities[selected_side])) - Decimal(
                        str(selected_execution.executable_cost)
                    )
                    if edge_decimal + EDGE_TOLERANCE < required_edge:
                        failures.append(
                            _failure(
                                "minimum_edge",
                                "Kelly-sized entry no longer meets edge floor after book walk",
                                selected_edge,
                                float(required_edge),
                            )
                        )
                except InsufficientDepthError as exc:
                    failures.append(
                        _failure(
                            f"{selected_side.value.lower()}_kelly_execution",
                            str(exc),
                            depth(market.orderbook, selected_side, asks=True),
                            kelly_qty,
                        )
                    )

        exit_bid_depth = depth(market.orderbook, selected_side, asks=False)
        if exit_bid_depth + 1e-12 < trade_quantity:
            failures.append(
                _failure(
                    "exit_liquidity",
                    "order book lacks bid depth to exit the proposed position",
                    exit_bid_depth,
                    trade_quantity,
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
                poll=poll_snapshot,
                cfg=poll_cfg,
            )
            if poll_failure is not None:
                failures.append(poll_failure)

        if failures:
            return DecisionResult(
                action=DecisionAction.NO_TRADE,
                reason=f"entry blocked by safety gates{_crowd_context_suffix(entry_ctx)}",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                selected_side=selected_side,
                predicted_probability=side_probabilities[selected_side],
                executable_cost=selected_execution.executable_cost,
                edge=selected_edge,
                target_edge=cfg.target_edge,
                required_edge=float(required_edge),
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
            reason=f"{selected_side.value} {target_text}{_crowd_context_suffix(entry_ctx)}",
            gate_failures=(),
            current_direction=current_direction,
            predicted_direction=predicted_direction,
            trade_direction=_direction_for_side(selected_side),
            selected_side=selected_side,
            predicted_probability=side_probabilities[selected_side],
            executable_cost=selected_execution.executable_cost,
            edge=selected_edge,
            target_edge=cfg.target_edge,
            required_edge=float(required_edge),
            quantity=trade_quantity,
            execution=selected_execution,
            size_multiplier=size_multiplier,
        )


def make_decision(
    market: MarketSnapshot,
    forecast: ProbabilityEstimate,
    features: FeatureSnapshot,
    benchmark: BenchmarkQuote,
    **kwargs: object,
) -> DecisionResult:
    return DecisionEngine().decide(market, forecast, features, benchmark, **kwargs)
