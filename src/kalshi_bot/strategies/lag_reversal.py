"""Kalshi lag reversal entries: score + mispricing required; score alone never trades."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from kalshi_bot.config import LagReversalConfig
from kalshi_bot.domain import ContractSide, FeatureSnapshot, MarketSnapshot, ProbabilityEstimate, Regime
from kalshi_bot.features.enriched import EnrichedFeatures
from kalshi_bot.market.orderbook import estimate_buy_execution
from kalshi_bot.strategies.alt_signal import AltTradeSignal
from kalshi_bot.strategies.reversal_score import (
    ReversalScoreAssessment,
    ReversalTier,
    compute_reversal_score,
    market_yes_poll_from_book,
)


@dataclass
class ReversalContextTracker:
    """Per-ticker prior model probability for detecting material probability flips."""

    _last_p_up: dict[str, float] = field(default_factory=dict)

    def prior_p_up(self, ticker: str) -> float | None:
        return self._last_p_up.get(ticker)

    def record(self, ticker: str, p_up: float) -> None:
        self._last_p_up[ticker] = p_up


@dataclass(frozen=True)
class LagReversalEvaluation:
    signal: AltTradeSignal | None
    assessment: ReversalScoreAssessment | None
    rationale: str


def reversal_setup_material(
    assessment: ReversalScoreAssessment,
    *,
    min_kalshi_lag: float,
    min_probability_change: float,
) -> bool:
    """True when Kalshi lag and model probability shift both exceed configured floors."""
    return (
        abs(assessment.kalshi_lag_on_reversal_side) + 1e-12 >= min_kalshi_lag
        and abs(assessment.probability_change) + 1e-12 >= min_probability_change
    )


def evaluate_lag_reversal(
    market: MarketSnapshot,
    *,
    features: FeatureSnapshot,
    enriched: EnrichedFeatures,
    forecast: ProbabilityEstimate,
    regime: Regime,
    cfg: LagReversalConfig,
    seconds_remaining: float,
    tracker: ReversalContextTracker | None = None,
    sweet_spot_min_seconds: float = 180.0,
    sweet_spot_max_seconds: float = 600.0,
) -> LagReversalEvaluation:
    if not cfg.enabled:
        return LagReversalEvaluation(None, None, "lag reversal disabled")

    if cfg.entry_enabled:
        if seconds_remaining < cfg.min_seconds_remaining:
            return LagReversalEvaluation(
                None,
                None,
                f"time remaining {seconds_remaining:.0f}s below minimum",
            )
        if seconds_remaining > cfg.max_seconds_remaining:
            return LagReversalEvaluation(
                None,
                None,
                f"time remaining {seconds_remaining:.0f}s above entry window",
            )

    prior = tracker.prior_p_up(market.ticker) if tracker is not None else None
    yes_poll = market_yes_poll_from_book(market.orderbook)
    assessment = compute_reversal_score(
        features,
        enriched,
        forecast,
        market_yes_poll=yes_poll,
        regime=regime,
        seconds_remaining=seconds_remaining,
        prior_p_up=prior,
        min_initial_move_z=cfg.min_initial_move_z,
        sweet_spot_min_seconds=sweet_spot_min_seconds,
        sweet_spot_max_seconds=sweet_spot_max_seconds,
    )
    if tracker is not None:
        tracker.record(market.ticker, forecast.p_up)

    if not cfg.entry_enabled:
        return LagReversalEvaluation(
            None,
            assessment,
            f"reversal signal only · {assessment.summary}",
        )

    if assessment.tier is ReversalTier.NONE:
        return LagReversalEvaluation(
            None,
            assessment,
            f"reversal score {assessment.score:.0f} — no reversal setup",
        )
    if assessment.score + 1e-9 < cfg.min_entry_score:
        return LagReversalEvaluation(
            None,
            assessment,
            f"reversal score {assessment.score:.0f} below entry threshold {cfg.min_entry_score:.0f}",
        )

    side = assessment.reversal_side
    prob = forecast.p_up if side is ContractSide.YES else forecast.p_down
    if prob + 1e-12 < cfg.min_reversal_probability:
        return LagReversalEvaluation(
            None,
            assessment,
            f"reversal-side probability {prob:.1%} below {cfg.min_reversal_probability:.1%}",
        )

    if abs(assessment.probability_change) + 1e-12 < cfg.min_probability_change:
        return LagReversalEvaluation(
            None,
            assessment,
            f"probability change {assessment.probability_change:+.1%} below "
            f"{cfg.min_probability_change:.1%}",
        )

    if assessment.kalshi_lag_on_reversal_side + 1e-12 < cfg.min_kalshi_lag:
        return LagReversalEvaluation(
            None,
            assessment,
            f"Kalshi lag {assessment.kalshi_lag_on_reversal_side:+.1%} below "
            f"{cfg.min_kalshi_lag:.1%}",
        )

    if cfg.require_cross_feed_confirm and features.cross_venue_agreement < cfg.min_cross_venue_agreement:
        return LagReversalEvaluation(
            None,
            assessment,
            f"cross-venue agreement {features.cross_venue_agreement:.1%} too low",
        )

    try:
        execution = estimate_buy_execution(market.orderbook, side, cfg.order_quantity)
    except Exception as exc:
        return LagReversalEvaluation(
            None,
            assessment,
            f"no executable depth: {exc}",
        )

    edge = prob - execution.executable_cost
    if edge + 1e-12 < cfg.min_edge:
        return LagReversalEvaluation(
            None,
            assessment,
            f"net edge {edge:.1%} below {cfg.min_edge:.1%} after costs",
        )

    price = min(0.99, execution.average_price + 0.01)
    rationale = (
        f"{assessment.summary}; executable {side.value} @ {execution.executable_cost:.1%} "
        f"edge {edge:.1%}"
    )
    signal = AltTradeSignal(
        strategy="lag_reversal",
        ticker=market.ticker,
        side=side,
        action="buy",
        quantity=cfg.order_quantity,
        limit_price=price,
        edge=edge,
        time_in_force="immediate_or_cancel",
        reason="kalshi lag reversal",
        intent_id=f"lagrev-{market.ticker}-{uuid.uuid4().hex[:8]}",
        rationale=rationale,
    )
    return LagReversalEvaluation(signal, assessment, rationale)
