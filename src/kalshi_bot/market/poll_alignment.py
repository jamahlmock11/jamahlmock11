"""Market poll (implied probability) alignment gates for late-window entries."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.domain import ContractSide, GateFailure, OrderBookSnapshot, ProbabilityEstimate


@dataclass(frozen=True)
class PollConfig:
    """Poll alignment thresholds for favoring market consensus."""

    mode: str = "legacy"
    confirm_threshold: float = 0.75
    favorable_min: float = 0.85
    favorable_max: float = 0.90
    low_poll_threshold: float = 0.85
    counter_evidence_min_probability: float = 0.70
    counter_evidence_min_confidence: float = 0.65
    counter_evidence_min_agreement: float = 0.65
    low_poll_min_probability: float = 0.72
    low_poll_min_confidence: float = 0.68
    low_poll_min_agreement: float = 0.68


@dataclass(frozen=True)
class PollSnapshot:
    yes_poll: float | None
    no_poll: float | None
    dominant_side: ContractSide | None
    dominant_poll: float | None


def _side_mid(book: OrderBookSnapshot, side: ContractSide) -> float | None:
    if side is ContractSide.YES:
        bid, ask = book.yes_bid, book.yes_ask
    else:
        bid, ask = book.no_bid, book.no_ask
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return ask if ask is not None else bid


def market_poll_snapshot(book: OrderBookSnapshot) -> PollSnapshot:
    yes_poll = _side_mid(book, ContractSide.YES)
    no_poll = _side_mid(book, ContractSide.NO)
    if yes_poll is None and no_poll is None:
        return PollSnapshot(None, None, None, None)
    if yes_poll is not None and no_poll is not None:
        if yes_poll >= no_poll:
            return PollSnapshot(yes_poll, no_poll, ContractSide.YES, yes_poll)
        return PollSnapshot(yes_poll, no_poll, ContractSide.NO, no_poll)
    if yes_poll is not None:
        return PollSnapshot(yes_poll, no_poll, ContractSide.YES, yes_poll)
    return PollSnapshot(yes_poll, no_poll, ContractSide.NO, no_poll)


def selected_side_poll(snapshot: PollSnapshot, side: ContractSide) -> float | None:
    if side is ContractSide.YES:
        return snapshot.yes_poll
    return snapshot.no_poll


def _failure(gate: str, reason: str, observed: object, required: object) -> GateFailure:
    return GateFailure(gate=gate, reason=reason, observed=observed, required=required)


def has_counter_evidence(
    forecast: ProbabilityEstimate,
    side: ContractSide,
    cfg: PollConfig,
) -> bool:
    """True when the model strongly favors a side against an extreme crowd poll."""
    prob = forecast.p_up if side is ContractSide.YES else forecast.p_down
    return (
        prob + 1e-12 >= cfg.counter_evidence_min_probability
        and forecast.confidence + 1e-12 >= cfg.counter_evidence_min_confidence
        and forecast.signal_agreement + 1e-12 >= cfg.counter_evidence_min_agreement
    )


_has_counter_evidence = has_counter_evidence


def _has_low_poll_evidence(
    forecast: ProbabilityEstimate,
    side: ContractSide,
    cfg: PollConfig,
) -> bool:
    prob = forecast.p_up if side is ContractSide.YES else forecast.p_down
    return (
        prob + 1e-12 >= cfg.low_poll_min_probability
        and forecast.confidence + 1e-12 >= cfg.low_poll_min_confidence
        and forecast.signal_agreement + 1e-12 >= cfg.low_poll_min_agreement
    )


def evaluate_poll_alignment(
    *,
    selected_side: ContractSide,
    forecast: ProbabilityEstimate,
    poll: PollSnapshot,
    cfg: PollConfig,
) -> GateFailure | None:
    """Return a gate failure when poll consensus conflicts with the selected side."""
    side_poll = selected_side_poll(poll, selected_side)
    if side_poll is None:
        return _failure(
            "poll_missing",
            "market poll is unavailable for the selected side",
            None,
            "executable poll quote",
        )

    dominant = poll.dominant_poll
    dominant_side = poll.dominant_side
    if (
        dominant is not None
        and dominant_side is not None
        and dominant + 1e-12 >= cfg.favorable_min
        and selected_side is not dominant_side
        and not _has_counter_evidence(forecast, selected_side, cfg)
    ):
        return _failure(
            "poll_contrarian",
            "trade opposes strong market poll without counter-evidence",
            (side_poll, dominant_side.value, dominant),
            (
                cfg.favorable_min,
                cfg.counter_evidence_min_probability,
                cfg.counter_evidence_min_confidence,
                cfg.counter_evidence_min_agreement,
            ),
        )

    if side_poll + 1e-12 < cfg.low_poll_threshold:
        if not _has_low_poll_evidence(forecast, selected_side, cfg):
            return _failure(
                "low_poll",
                "selected side poll is below threshold without sufficient model evidence",
                side_poll,
                (
                    cfg.low_poll_threshold,
                    cfg.low_poll_min_probability,
                    cfg.low_poll_min_confidence,
                    cfg.low_poll_min_agreement,
                ),
            )

    return None


def evaluate_poll_confirmation(
    *,
    selected_side: ContractSide,
    forecast: ProbabilityEstimate,
    poll: PollSnapshot,
    threshold: float,
) -> GateFailure | None:
    """When crowd poll is high, require the model to agree on the same side."""
    side_poll = selected_side_poll(poll, selected_side)
    if side_poll is None:
        return _failure(
            "poll_missing",
            "market poll is unavailable for the selected side",
            None,
            "executable poll quote",
        )

    model_prob = forecast.p_up if selected_side is ContractSide.YES else forecast.p_down
    if side_poll + 1e-12 >= threshold and model_prob + 1e-12 < threshold:
        return _failure(
            "poll_confirm",
            "high market poll requires matching model conviction on the same side",
            (side_poll, model_prob),
            threshold,
        )
    return None


def poll_gate_config_from_model(model: object) -> PollConfig:
    """Convert the YAML/pydantic poll config into gate-evaluation settings."""
    return PollConfig(
        mode=str(getattr(model, "mode", "legacy")),
        confirm_threshold=float(getattr(model, "confirm_threshold", 0.75)),
        favorable_min=float(getattr(model, "favorable_min", 0.85)),
        favorable_max=float(getattr(model, "favorable_max", 0.90)),
        low_poll_threshold=float(getattr(model, "low_poll_threshold", 0.85)),
        counter_evidence_min_probability=float(
            getattr(model, "counter_evidence_min_probability", 0.70)
        ),
        counter_evidence_min_confidence=float(
            getattr(model, "counter_evidence_min_confidence", 0.65)
        ),
        counter_evidence_min_agreement=float(
            getattr(model, "counter_evidence_min_agreement", 0.65)
        ),
        low_poll_min_probability=float(getattr(model, "low_poll_min_probability", 0.72)),
        low_poll_min_confidence=float(getattr(model, "low_poll_min_confidence", 0.68)),
        low_poll_min_agreement=float(getattr(model, "low_poll_min_agreement", 0.68)),
    )


def evaluate_poll_gate(
    *,
    selected_side: ContractSide,
    forecast: ProbabilityEstimate,
    poll: PollSnapshot,
    cfg: PollConfig,
) -> GateFailure | None:
    if cfg.mode == "disabled":
        return None
    if cfg.mode == "confirm_aligned":
        return evaluate_poll_confirmation(
            selected_side=selected_side,
            forecast=forecast,
            poll=poll,
            threshold=cfg.confirm_threshold,
        )
    return evaluate_poll_alignment(
        selected_side=selected_side,
        forecast=forecast,
        poll=poll,
        cfg=cfg,
    )
