"""Rank multiple hourly strike candidates and pick the best trade opportunity."""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_bot.domain import DecisionAction, DecisionResult, MarketSnapshot
from kalshi_bot.hour.mispricing import MispricingAssessment
from kalshi_bot.hour.terminal_probability import TerminalForecast


@dataclass(frozen=True)
class StrikeCandidateResult:
    market: MarketSnapshot
    terminal: TerminalForecast
    decision: DecisionResult
    mispricing: MispricingAssessment | None
    stability_swing: float
    rank_key: tuple
    summary: str


def _price_band_score(mispricing: MispricingAssessment | None) -> int:
    if mispricing is None:
        return 0
    score = 0
    if mispricing.yes is not None and 0.49 <= mispricing.yes.executable_cost <= 0.51:
        score += 1
    if mispricing.no is not None and 0.49 <= mispricing.no.executable_cost <= 0.51:
        score += 1
    return score


def rank_terminal_candidate(
    *,
    market: MarketSnapshot,
    decision: DecisionResult,
    mispricing: MispricingAssessment | None,
    has_position: bool,
) -> tuple:
    """Higher sort key = better candidate. Positioned markets win for hold/exit."""
    if decision.action in {DecisionAction.BUY_UP, DecisionAction.BUY_DOWN}:
        tier = 4
    elif decision.action is DecisionAction.EXIT:
        tier = 3
    elif decision.action is DecisionAction.HOLD and has_position:
        tier = 3
    elif mispricing is not None and mispricing.best_net_edge + 1e-12 >= mispricing.required_edge:
        tier = 2
    elif _price_band_score(mispricing) > 0:
        tier = 1
    else:
        tier = 0

    edge = decision.edge
    if edge is None and mispricing is not None:
        edge = mispricing.best_net_edge
    edge = edge if edge is not None else -1.0

    required = mispricing.required_edge if mispricing is not None else 1.0
    edge_gap = max(0.0, required - edge)
    price_score = _price_band_score(mispricing)
    gate_penalty = len(decision.gate_failures)

    return (
        1 if has_position else 0,
        tier,
        price_score,
        edge,
        -edge_gap,
        -gate_penalty,
        -abs(market.strike),
    )


def candidate_summary(
    market: MarketSnapshot,
    decision: DecisionResult,
    mispricing: MispricingAssessment | None,
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
    return (
        f"{strike} {action} · YES edge {yes_edge} · NO edge {no_edge} · "
        f"need {mispricing.required_edge * 100:.0f}pp"
    )


def select_best_strike_candidate(
    candidates: list[StrikeCandidateResult],
) -> StrikeCandidateResult | None:
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.rank_key)
