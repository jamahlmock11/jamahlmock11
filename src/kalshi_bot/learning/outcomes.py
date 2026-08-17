"""Wire exit/settlement outcomes into continuous learning stores."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kalshi_bot.domain import ContractSide, FeatureSnapshot, MarketSnapshot
from kalshi_bot.intelligence.signals import compute_technical_signals
from kalshi_bot.journal import TradeJournal
from kalshi_bot.learning.pattern_matcher import PatternMatcher
from kalshi_bot.learning.signal_weights import SignalWeightTracker
from kalshi_bot.learning.trade_recorder import TradeRecorder

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoundTripOutcome:
    ticker: str
    held_side: ContractSide
    pnl: float
    round_trip_win: bool
    actual_up: bool
    outcome: float


def round_trip_outcome(
    *,
    ticker: str,
    held_side: ContractSide,
    pnl: float,
) -> RoundTripOutcome:
    """Translate a closed position into learning labels."""
    round_trip_win = pnl > 0
    if round_trip_win:
        actual_up = held_side is ContractSide.YES
    else:
        actual_up = held_side is ContractSide.NO
    return RoundTripOutcome(
        ticker=ticker,
        held_side=held_side,
        pnl=pnl,
        round_trip_win=round_trip_win,
        actual_up=actual_up,
        outcome=1.0 if round_trip_win else 0.0,
    )


def record_round_trip_learning(
    *,
    ticker: str,
    held_side: ContractSide,
    pnl: float,
    trade_recorder: TradeRecorder,
    pattern_matcher: PatternMatcher,
    journal: TradeJournal,
    signal_weights: SignalWeightTracker,
    signal_weights_path: Path,
    features: FeatureSnapshot | None = None,
    market: MarketSnapshot | None = None,
    component_probabilities: dict[str, float] | None = None,
) -> RoundTripOutcome:
    """Persist win/loss labels so pattern, temporal, and signal learning can use them."""
    labels = round_trip_outcome(ticker=ticker, held_side=held_side, pnl=pnl)

    trade_recorder.record_outcome(
        ticker,
        outcome=labels.outcome,
        pnl=pnl,
    )
    pattern_matcher.record_outcome_for_ticker(
        ticker,
        outcome=labels.outcome,
        pnl=pnl,
    )
    journal.label_latest_entry(
        ticker,
        outcome=labels.outcome,
        pnl=pnl,
    )

    signal_probs: dict[str, float] = {}
    if features is not None:
        signals = compute_technical_signals(features, market.orderbook if market else None)
        signal_probs = {
            "ema": signals.ema,
            "rsi": signals.rsi,
            "vwap": signals.vwap,
            "bollinger": signals.bollinger,
            "orderbook": signals.orderbook,
            "news": signals.news,
        }
    elif component_probabilities:
        signal_probs = {
            key: value
            for key, value in component_probabilities.items()
            if isinstance(value, (int, float))
        }

    if signal_probs:
        signal_weights.record_signal_outcomes(signal_probs, labels.actual_up)
        if signal_weights.should_update():
            signal_weights.update_weights()
            signal_weights.save(signal_weights_path)

    logger.info(
        "Recorded learning outcome %s %s pnl=%.2f win=%s",
        ticker,
        held_side.value,
        pnl,
        labels.round_trip_win,
    )
    return labels
