"""Terminal mispricing decision engine for the live 1-hour bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshi_bot.config import TerminalProbabilityConfig
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
from kalshi_bot.execution.position_reversal import PositionReversalConfig, reversal_config_from_risk
from kalshi_bot.execution.stop_loss import evaluate_position_exit
from kalshi_bot.hour.mispricing import assess_mispricing, liquidity_ok
from kalshi_bot.hour.terminal_probability import TerminalForecast, TerminalProbabilityEngine
from kalshi_bot.market.orderbook import (
    InsufficientDepthError,
    depth,
    estimate_buy_execution,
    spread,
)
from kalshi_bot.strategies.decision import _direction_for_side, _failure


@dataclass(frozen=True)
class TerminalDecisionConfig:
    terminal: TerminalProbabilityConfig
    quantity: float = 1.0
    maximum_benchmark_age: float = 20.0
    maximum_feature_age: float = 15.0
    minimum_seconds_remaining: float = 60.0
    maximum_seconds_remaining: float = 2400.0
    minimum_data_completeness: float = 0.65
    fee_rate: float = 0.0
    fee_per_contract: float = 0.0
    slippage_bps: float = 5.0
    slippage_per_contract: float = 0.0
    allow_replay_data: bool = False
    allow_proxy_data: bool = False
    proxy_minimum_constituents: int = 3
    proxy_maximum_dispersion: float = 0.003
    proxy_entry_cutoff_seconds: float = 300.0
    stop_loss_fraction: float = 0.55
    opposite_edge_shift: float = 0.25
    thesis_reversal_margin: float = 0.10
    thesis_reversal_enabled: bool = False
    opposite_edge_exit_enabled: bool = False
    recovery_hold_enabled: bool = False
    recovery_hold_min_probability: float = 0.58
    recovery_hold_min_confidence: float = 0.58
    recovery_hold_min_agreement: float = 0.58
    min_hold_seconds: float = 120.0
    position_reversal: PositionReversalConfig = field(default_factory=PositionReversalConfig)


@dataclass
class ProbabilityStabilityTracker:
    max_swing: float = 0.08
    _history: list[float] = field(default_factory=list)
    _limit: int = 5

    def observe(self, p_yes: float) -> tuple[bool, float]:
        self._history.append(p_yes)
        if len(self._history) > self._limit:
            self._history = self._history[-self._limit:]
        if len(self._history) < 2:
            return True, 0.0
        swing = max(self._history) - min(self._history)
        return swing <= self.max_swing, swing


def format_terminal_explanation(
    terminal: TerminalForecast,
    mispricing,
    decision: DecisionResult,
    *,
    calibration_pass: bool,
    liquidity_pass: bool,
    stability_swing: float,
) -> str:
    yes_ask = (
        f"{mispricing.yes.ask_price * 100:.0f}¢"
        if mispricing.yes is not None and mispricing.yes.ask_price is not None
        else "—"
    )
    no_ask = (
        f"{mispricing.no.ask_price * 100:.0f}¢"
        if mispricing.no is not None and mispricing.no.ask_price is not None
        else "—"
    )
    yes_edge = (
        f"{mispricing.yes_net_edge * 100:.1f}pp"
        if mispricing.yes_net_edge is not None
        else "—"
    )
    no_edge = (
        f"{mispricing.no_net_edge * 100:.1f}pp"
        if mispricing.no_net_edge is not None
        else "—"
    )
    mins = terminal.seconds_remaining / 60.0
    action_label = decision.action.value.replace("_", " ")
    return (
        f"Strike: ${terminal.strike:,.0f}\n"
        f"Current BRTI: ${terminal.current_brti:,.0f}\n"
        f"Time remaining: {mins:.0f}m\n"
        f"Expected terminal BRTI: ${terminal.expected_terminal_brti:,.0f}\n"
        f"P(YES): {terminal.calibrated_p_yes:.1%}\n"
        f"P(NO): {terminal.calibrated_p_no:.1%}\n"
        f"YES ask: {yes_ask}\n"
        f"NO ask: {no_ask}\n"
        f"Net YES edge: {yes_edge}\n"
        f"Net NO edge: {no_edge}\n"
        f"Required edge: {mispricing.required_edge * 100:.1f}pp\n"
        f"Confidence: {terminal.confidence:.1%}\n"
        f"Agreement: {terminal.signal_agreement:.1%}\n"
        f"Stability swing: {stability_swing * 100:.1f}pp\n"
        f"Calibration: {'PASS' if calibration_pass else 'WARN'}\n"
        f"Liquidity: {'PASS' if liquidity_pass else 'FAIL'}\n"
        f"Decision: {action_label}"
    )


class HourTerminalDecisionEngine:
    """Mispricing-first hourly decisions using calibrated terminal probabilities."""

    def __init__(self, config: TerminalDecisionConfig | None = None) -> None:
        self.config = config or TerminalDecisionConfig(
            terminal=TerminalProbabilityConfig(enabled=True)
        )
        self.stability_tracker = ProbabilityStabilityTracker(
            max_swing=self.config.terminal.probability_stability_max_swing,
        )

    def _verify_contract(
        self,
        market: MarketSnapshot,
        terminal: TerminalForecast,
        features: FeatureSnapshot,
    ) -> list[GateFailure]:
        failures: list[GateFailure] = []
        if market.strike is None or market.strike <= 0:
            failures.append(
                _failure(
                    "contract_strike",
                    "actual Kalshi strike is missing",
                    market.strike,
                    "positive strike",
                )
            )
        if abs(market.strike - terminal.strike) > 0.01:
            failures.append(
                _failure(
                    "strike_mismatch",
                    "feature strike does not match market strike",
                    (market.strike, terminal.strike),
                    "matching strike",
                )
            )
        if features.strike != terminal.strike:
            failures.append(
                _failure(
                    "feature_strike",
                    "computed features use a different strike than the contract",
                    features.strike,
                    terminal.strike,
                )
            )
        reference = f"{market.reference or ''} {market.rules or ''}".lower()
        if "brti" not in reference and "bitcoin real time index" not in reference:
            failures.append(
                _failure(
                    "settlement_reference",
                    "settlement rules do not reference BRTI",
                    reference,
                    "CME CF BRTI",
                )
            )
        return failures

    def _common_gates(
        self,
        market: MarketSnapshot,
        terminal: TerminalForecast,
        features: FeatureSnapshot,
        benchmark: BenchmarkQuote,
        now: datetime,
        *,
        risk_locked: bool,
        duplicate_entry: bool,
    ) -> list[GateFailure]:
        cfg = self.config
        tcfg = cfg.terminal
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
        if terminal.confidence + 1e-12 < tcfg.minimum_confidence:
            failures.append(
                _failure(
                    "confidence",
                    "terminal confidence below minimum",
                    terminal.confidence,
                    tcfg.minimum_confidence,
                )
            )
        if terminal.signal_agreement + 1e-12 < tcfg.minimum_ensemble:
            failures.append(
                _failure(
                    "agreement",
                    "ensemble agreement below minimum",
                    terminal.signal_agreement,
                    tcfg.minimum_ensemble,
                )
            )
        liq_ok, liq_reason = liquidity_ok(
            market.orderbook,
            max_spread=tcfg.max_spread,
            quantity=cfg.quantity,
            require_depth=tcfg.require_orderbook_depth,
        )
        if not liq_ok:
            failures.append(
                _failure(
                    "liquidity",
                    liq_reason,
                    spread(market.orderbook, ContractSide.YES),
                    tcfg.max_spread,
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
        failures.extend(self._verify_contract(market, terminal, features))
        return failures

    @staticmethod
    def _can_exit(market: MarketSnapshot, side: ContractSide, quantity: float) -> bool:
        return depth(market.orderbook, side, asks=False) + 1e-12 >= quantity

    def _thesis_invalid(
        self,
        terminal: TerminalForecast,
        position_side: ContractSide,
    ) -> bool:
        tcfg = self.config.terminal
        held_prob = (
            terminal.calibrated_p_yes
            if position_side is ContractSide.YES
            else terminal.calibrated_p_no
        )
        opposite_prob = (
            terminal.calibrated_p_no
            if position_side is ContractSide.YES
            else terminal.calibrated_p_yes
        )
        if held_prob + 1e-12 < tcfg.thesis_invalid_min_probability:
            return True
        if opposite_prob - held_prob >= tcfg.thesis_invalid_margin:
            return True
        return False

    def decide(
        self,
        market: MarketSnapshot,
        terminal: TerminalForecast,
        features: FeatureSnapshot,
        benchmark: BenchmarkQuote,
        *,
        now: datetime | None = None,
        risk_locked: bool = False,
        duplicate_entry: bool = False,
        quantity: float | None = None,
        risk_manager=None,
        calibration_pass: bool = True,
        regime: Regime | None = None,
    ) -> tuple[DecisionResult, object, float]:
        observed_now = utc_datetime(now or datetime.now(timezone.utc))
        cfg = self.config
        tcfg = cfg.terminal
        trade_quantity = quantity if quantity is not None else cfg.quantity
        predicted_side = (
            ContractSide.YES
            if terminal.calibrated_p_yes >= terminal.calibrated_p_no
            else ContractSide.NO
        )
        predicted_direction = _direction_for_side(predicted_side)
        position = market.current_position
        current_direction = _direction_for_side(position.side if position else None)
        failures = self._common_gates(
            market,
            terminal,
            features,
            benchmark,
            observed_now,
            risk_locked=risk_locked,
            duplicate_entry=duplicate_entry,
        )

        forecast = terminal.as_probability_estimate(regime=regime)

        if position is not None and position.quantity > 0:
            if self._thesis_invalid(terminal, position.side):
                exit_quantity = min(position.quantity, trade_quantity)
                if self._can_exit(market, position.side, exit_quantity):
                    return (
                        DecisionResult(
                            action=DecisionAction.EXIT,
                            reason="terminal thesis invalidated: calibrated expiration probability no longer supports held side",
                            gate_failures=tuple(failures),
                            current_direction=current_direction,
                            predicted_direction=predicted_direction,
                            trade_direction=Direction.FLAT,
                            selected_side=position.side,
                            predicted_probability=(
                                terminal.calibrated_p_yes
                                if position.side is ContractSide.YES
                                else terminal.calibrated_p_no
                            ),
                            quantity=exit_quantity,
                            target_edge=tcfg.fallback_min_edge,
                        ),
                        None,
                        0.0,
                    )
            exit_signal = evaluate_position_exit(
                market=market,
                position=position,
                forecast=forecast,
                features=features,
                failures=failures,
                predicted_side=predicted_side,
                quantity=trade_quantity,
                stop_loss_fraction=cfg.stop_loss_fraction,
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
                now=observed_now,
            )
            if exit_signal is not None:
                exit_quantity = min(position.quantity, trade_quantity)
                if self._can_exit(market, position.side, exit_quantity):
                    return (
                        DecisionResult(
                            action=DecisionAction.EXIT,
                            reason=exit_signal.reason,
                            gate_failures=tuple(failures),
                            current_direction=current_direction,
                            predicted_direction=predicted_direction,
                            trade_direction=Direction.FLAT,
                            selected_side=position.side,
                            predicted_probability=(
                                terminal.calibrated_p_yes
                                if position.side is ContractSide.YES
                                else terminal.calibrated_p_no
                            ),
                            quantity=exit_quantity,
                            target_edge=tcfg.fallback_min_edge,
                        ),
                        None,
                        0.0,
                    )
            return (
                DecisionResult(
                    action=DecisionAction.HOLD,
                    reason="holding; terminal expiration thesis still valid",
                    gate_failures=tuple(failures),
                    current_direction=current_direction,
                    predicted_direction=predicted_direction,
                    trade_direction=Direction.FLAT,
                    selected_side=position.side,
                    predicted_probability=(
                        terminal.calibrated_p_yes
                        if position.side is ContractSide.YES
                        else terminal.calibrated_p_no
                    ),
                    quantity=position.quantity,
                    target_edge=tcfg.fallback_min_edge,
                ),
                None,
                0.0,
            )

        mispricing = assess_mispricing(
            terminal,
            market.orderbook,
            quantity=trade_quantity,
            cfg=tcfg,
            fee_rate=cfg.fee_rate,
            fee_per_contract=cfg.fee_per_contract,
            slippage_bps=cfg.slippage_bps,
            slippage_per_contract=cfg.slippage_per_contract,
        )

        stable, stability_swing = True, 0.0
        if tcfg.probability_stability_enabled:
            stable, stability_swing = self.stability_tracker.observe(
                terminal.calibrated_p_yes
            )
            if not stable:
                failures.append(
                    _failure(
                        "probability_stability",
                        "terminal probability swing exceeds stability limit",
                        stability_swing,
                        tcfg.probability_stability_max_swing,
                    )
                )

        if not calibration_pass and tcfg.calibration.enabled:
            failures.append(
                _failure(
                    "calibration",
                    "historical calibration buckets show material over/under-confidence",
                    False,
                    True,
                )
            )

        selected_side = mispricing.best_side
        selected_mispricing = (
            mispricing.yes if selected_side is ContractSide.YES else mispricing.no
        )
        if selected_side is None or selected_mispricing is None:
            failures.append(
                _failure(
                    "execution",
                    "neither side has executable depth for mispricing evaluation",
                    None,
                    "executable book",
                )
            )
            decision = DecisionResult(
                action=DecisionAction.NO_TRADE,
                reason="no executable mispricing on either side",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                target_edge=tcfg.fallback_min_edge,
                required_edge=mispricing.required_edge,
            )
            return decision, mispricing, stability_swing

        if selected_mispricing.net_edge + 1e-12 < mispricing.required_edge:
            failures.append(
                _failure(
                    "minimum_edge",
                    "best net edge below dynamic minimum after costs",
                    selected_mispricing.net_edge,
                    mispricing.required_edge,
                )
            )

        if tcfg.min_entry_executable_cost > 0 and (
            selected_mispricing.executable_cost + 1e-12 < tcfg.min_entry_executable_cost
        ):
            failures.append(
                _failure(
                    "min_entry_price",
                    "executable entry price below configured floor",
                    selected_mispricing.executable_cost,
                    tcfg.min_entry_executable_cost,
                )
            )

        if tcfg.forecast_alignment:
            side_prob = selected_mispricing.model_probability
            if side_prob + 1e-12 < tcfg.forecast_alignment_min_probability:
                failures.append(
                    _failure(
                        "forecast_alignment",
                        "calibrated terminal probability below alignment minimum on selected side",
                        side_prob,
                        tcfg.forecast_alignment_min_probability,
                    )
                )

        try:
            selected_execution = estimate_buy_execution(
                market.orderbook,
                selected_side,
                trade_quantity,
                fee_rate=cfg.fee_rate,
                fee_per_contract=cfg.fee_per_contract,
                slippage_bps=cfg.slippage_bps,
                slippage_per_contract=cfg.slippage_per_contract,
            )
        except InsufficientDepthError as exc:
            failures.append(
                _failure(
                    f"{selected_side.value.lower()}_execution",
                    str(exc),
                    depth(market.orderbook, selected_side, asks=True),
                    trade_quantity,
                )
            )
            selected_execution = None

        if selected_execution is not None and risk_manager is not None:
            if risk_manager.config.risk.kelly_enabled:
                kelly_qty = risk_manager.kelly_contracts_for_entry(
                    edge=selected_mispricing.net_edge,
                    executable_cost=selected_execution.executable_cost,
                    size_multiplier=1.0,
                    ticker=market.ticker,
                    min_edge=mispricing.required_edge,
                )
                if kelly_qty <= 0:
                    failures.append(
                        _failure(
                            "kelly_sizing",
                            "Kelly sizing produced zero affordable contracts",
                            selected_mispricing.net_edge,
                            mispricing.required_edge,
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
                        mispricing = assess_mispricing(
                            terminal,
                            market.orderbook,
                            quantity=trade_quantity,
                            cfg=tcfg,
                            fee_rate=cfg.fee_rate,
                            fee_per_contract=cfg.fee_per_contract,
                            slippage_bps=cfg.slippage_bps,
                            slippage_per_contract=cfg.slippage_per_contract,
                        )
                        selected_mispricing = (
                            mispricing.yes
                            if selected_side is ContractSide.YES
                            else mispricing.no
                        )
                        if (
                            selected_mispricing is None
                            or selected_mispricing.net_edge + 1e-12 < mispricing.required_edge
                        ):
                            failures.append(
                                _failure(
                                    "minimum_edge",
                                    "Kelly-sized entry no longer meets net edge floor",
                                    selected_mispricing.net_edge if selected_mispricing else None,
                                    mispricing.required_edge,
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

        if failures:
            decision = DecisionResult(
                action=DecisionAction.NO_TRADE,
                reason="terminal mispricing blocked by safety gates or insufficient net edge",
                gate_failures=tuple(failures),
                current_direction=current_direction,
                predicted_direction=predicted_direction,
                trade_direction=Direction.FLAT,
                selected_side=selected_side,
                predicted_probability=selected_mispricing.model_probability,
                executable_cost=selected_mispricing.executable_cost,
                edge=selected_mispricing.net_edge,
                target_edge=tcfg.fallback_min_edge,
                required_edge=mispricing.required_edge,
                quantity=trade_quantity,
                execution=selected_execution,
            )
            return decision, mispricing, stability_swing

        action = (
            DecisionAction.BUY_UP
            if selected_side is ContractSide.YES
            else DecisionAction.BUY_DOWN
        )
        decision = DecisionResult(
            action=action,
            reason=(
                f"terminal mispricing: {selected_side.value} net edge "
                f"{selected_mispricing.net_edge:.1%} >= required {mispricing.required_edge:.1%}"
            ),
            gate_failures=(),
            current_direction=current_direction,
            predicted_direction=predicted_direction,
            trade_direction=_direction_for_side(selected_side),
            selected_side=selected_side,
            predicted_probability=selected_mispricing.model_probability,
            executable_cost=selected_mispricing.executable_cost,
            edge=selected_mispricing.net_edge,
            target_edge=tcfg.fallback_min_edge,
            required_edge=mispricing.required_edge,
            quantity=trade_quantity,
            execution=selected_execution,
        )
        return decision, mispricing, stability_swing


def terminal_decision_config_from_app(config) -> TerminalDecisionConfig:
    from kalshi_bot.config import AppConfig
    from kalshi_bot.execution.position_reversal import reversal_config_from_risk

    app: AppConfig = config
    hour = app.hour
    terminal = app.terminal_probability
    return TerminalDecisionConfig(
        terminal=terminal,
        quantity=app.strategy.order_quantity,
        maximum_benchmark_age=app.data.max_brti_age_seconds,
        minimum_seconds_remaining=hour.min_seconds_remaining,
        maximum_seconds_remaining=hour.max_entry_seconds_remaining,
        minimum_data_completeness=app.strategy.min_data_completeness,
        fee_rate=app.execution.fee_rate,
        fee_per_contract=app.execution.fee_per_contract,
        slippage_bps=app.execution.slippage_bps,
        slippage_per_contract=app.execution.slippage_per_contract,
        allow_proxy_data=(
            app.execution.dry_run and app.data.benchmark_mode == "constituent_proxy"
        ),
        proxy_minimum_constituents=app.data.min_supporting_venues,
        proxy_maximum_dispersion=app.data.max_supporting_dispersion,
        stop_loss_fraction=app.risk.stop_loss_fraction,
        opposite_edge_shift=app.risk.opposite_edge_shift,
        thesis_reversal_margin=app.risk.thesis_reversal_margin,
        thesis_reversal_enabled=app.risk.thesis_reversal_enabled,
        opposite_edge_exit_enabled=app.risk.opposite_edge_exit_enabled,
        recovery_hold_enabled=app.risk.recovery_hold_enabled,
        recovery_hold_min_probability=app.risk.recovery_hold_min_probability,
        recovery_hold_min_confidence=app.risk.recovery_hold_min_confidence,
        recovery_hold_min_agreement=app.risk.recovery_hold_min_agreement,
        min_hold_seconds=app.risk.min_hold_seconds,
        position_reversal=reversal_config_from_risk(app.risk),
    )
