"""Per-contract state for detecting momentum exhaustion and probability flips."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from kalshi_bot.domain import Direction


@dataclass
class ContractReversalState:
    ticker: str
    initial_direction: Direction | None = None
    established: bool = False
    peak_model_prob: float = 0.0
    peak_kalshi_poll: float = 0.0
    initial_trend_strength: float = 0.0
    last_model_up: float = 0.5
    last_model_down: float = 0.5
    last_yes_poll: float | None = None
    last_orderbook_imbalance: float = 0.0
    polls_seen: int = 0


class ReversalStateTracker:
    """Track the initial strong move and peak probabilities for one contract."""

    def __init__(self) -> None:
        self._states: dict[str, ContractReversalState] = {}

    def get(self, ticker: str) -> ContractReversalState:
        if ticker not in self._states:
            self._states[ticker] = ContractReversalState(ticker=ticker)
        return self._states[ticker]

    def reset(self, ticker: str) -> None:
        self._states.pop(ticker, None)

    def prune(self, active_tickers: set[str]) -> None:
        for ticker in list(self._states):
            if ticker not in active_tickers:
                del self._states[ticker]

    def update(
        self,
        *,
        ticker: str,
        initial_direction: Direction | None,
        model_up: float,
        model_down: float,
        yes_poll: float | None,
        trend_strength: float,
        trend_consistency: float,
        orderbook_imbalance: float,
        min_consistency: float,
        min_strength: float,
    ) -> ContractReversalState:
        state = self.get(ticker)
        state.polls_seen += 1
        state.last_model_up = model_up
        state.last_model_down = model_down
        state.last_yes_poll = yes_poll
        state.last_orderbook_imbalance = orderbook_imbalance

        if initial_direction is None:
            return state

        favored_prob = model_up if initial_direction is Direction.UP else model_down
        favored_poll = yes_poll if initial_direction is Direction.UP else (
            (1.0 - yes_poll) if yes_poll is not None else None
        )

        if not state.established:
            if trend_consistency + 1e-12 >= min_consistency and trend_strength + 1e-12 >= min_strength:
                state.established = True
                state.initial_direction = initial_direction
                state.initial_trend_strength = trend_strength
                state.peak_model_prob = favored_prob
                if favored_poll is not None:
                    state.peak_kalshi_poll = favored_poll
            return state

        if state.initial_direction is not None and initial_direction is not state.initial_direction:
            return state

        state.peak_model_prob = max(state.peak_model_prob, favored_prob)
        if favored_poll is not None:
            state.peak_kalshi_poll = max(state.peak_kalshi_poll, favored_poll)
        return state
