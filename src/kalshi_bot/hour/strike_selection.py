"""Rank multiple hourly strike candidates and pick the best trade opportunity."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.domain import ContractSide, DecisionAction, DecisionResult, MarketSnapshot
from kalshi_bot.hour.mispricing import MispricingAssessment
from kalshi_bot.hour.terminal_probability import TerminalForecast


@dataclass(frozen=True)
class StrikeRankConfig:
    strong_evidence_min_probability: float = 0.78
    strong_evidence_min_confidence: float = 0.60
    strong_evidence_min_agreement: float = 0.55
    favorite_min_executable_cost: float = 0.60
    favorite_max_executable_cost: float = 0.80


@dataclass(frozen=True)
class StrikeCandidateResult:
    market: MarketSnapshot
    terminal: TerminalForecast
    decision: DecisionResult
    mispricing: MispricingAssessment | None
    stability_swing: float
    rank_key: tuple
    summary: str


def _price_band_score(
    mispricing: MispricingAssessment | None,
    *,
    favorite_min: float,
    favorite_max: float,
) -> int:
    """Score favorite-band executable prices (excludes coin-flip and longshot bands)."""
    if mispricing is None:
        return 0
    score = 0
    if mispricing.yes is not None and favorite_min <= mispricing.yes.executable_cost <= favorite_max:
        score += 1
    if mispricing.no is not None and favorite_min <= mispricing.no.executable_cost <= favorite_max:
        score += 1
    return score


def _selected_side_probability(
    terminal: TerminalForecast | None,
    mispricing: MispricingAssessment | None,
) -> float:
    if terminal is None or mispricing is None or mispricing.best_side is None:
        return 0.0
    if mispricing.best_side is ContractSide.YES:
        return terminal.calibrated_p_yes
    return terminal.calibrated_p_no


def _strong_evidence(
    terminal: TerminalForecast | None,
    mispricing: MispricingAssessment | None,
    cfg: StrikeRankConfig,
) -> bool:
    if terminal is None or mispricing is None or mispricing.best_side is None:
        return False
    side_prob = _selected_side_probability(terminal, mispricing)
    return (
        side_prob + 1e-12 >= cfg.strong_evidence_min_probability
        and terminal.confidence + 1e-12 >= cfg.strong_evidence_min_confidence
        and terminal.signal_agreement + 1e-12 >= cfg.strong_evidence_min_agreement
    )


def rank_terminal_candidate(
    *,
    market: MarketSnapshot,
    decision: DecisionResult,
    mispricing: MispricingAssessment | None,
    terminal: TerminalForecast | None = None,
    has_position: bool,
    rank_cfg: StrikeRankConfig | None = None,
) -> tuple:
    """Higher sort key = better candidate. Positioned markets win for hold/exit."""
    cfg = rank_cfg or StrikeRankConfig()
    strong = _strong_evidence(terminal, mispricing, cfg)
    side_prob = _selected_side_probability(terminal, mispricing)

    if decision.action in {DecisionAction.BUY_UP, DecisionAction.BUY_DOWN}:
        tier = 5 if strong else 4
    elif decision.action is DecisionAction.EXIT:
        tier = 3
    elif decision.action is DecisionAction.HOLD and has_position:
        tier = 3
    elif mispricing is not None and mispricing.best_net_edge + 1e-12 >= mispricing.required_edge:
        tier = 3 if strong else 2
    elif strong:
        tier = 2
    elif _price_band_score(
        mispricing,
        favorite_min=cfg.favorite_min_executable_cost,
        favorite_max=cfg.favorite_max_executable_cost,
    ) > 0:
        tier = 1
    else:
        tier = 0

    edge = decision.edge
    if edge is None and mispricing is not None:
        edge = mispricing.best_net_edge
    edge = edge if edge is not None else -1.0

    required = mispricing.required_edge if mispricing is not None else 1.0
    edge_gap = max(0.0, required - edge)
    price_score = _price_band_score(
        mispricing,
        favorite_min=cfg.favorite_min_executable_cost,
        favorite_max=cfg.favorite_max_executable_cost,
    )
    gate_penalty = len(decision.gate_failures)

    return (
        1 if has_position else 0,
        tier,
        1 if strong else 0,
        side_prob,
        price_score,
        edge,
        -edge_gap,
        -gate_penalty,
        market.strike,
    )


def candidate_summary(
    market: MarketSnapshot,
    decision: DecisionResult,
    mispricing: MispricingAssessment | None,
    terminal: TerminalForecast | None = None,
) -> str:
    strike = f"${market.strike:,.0f}"
    action = decision.action.value
    if mispricing is None:
        return f"{strike} {action}"
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
    strong_tag = ""
    if terminal is not None and _strong_evidence(terminal, mispricing, StrikeRankConfig()):
        side_prob = _selected_side_probability(terminal, mispricing)
        strong_tag = f" · strong {side_prob:.0%}"
    return (
        f"{strike} {action}{strong_tag} · YES edge {yes_edge} · NO edge {no_edge} · "
        f"need {mispricing.required_edge * 100:.0f}pp"
    )


def select_best_strike_candidate(
    candidates: list[StrikeCandidateResult],
) -> StrikeCandidateResult | None:
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.rank_key)
