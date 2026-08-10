"""Tests for market poll alignment gates."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kalshi_bot.domain import ContractSide, ProbabilityEstimate, Regime
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.market.poll_alignment import (
    PollConfig,
    evaluate_poll_alignment,
    evaluate_poll_confirmation,
    market_poll_snapshot,
)

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
CFG = PollConfig()


def book(yes_ask: float):
    yes_bid = yes_ask - 0.02
    no_bid = 1.0 - yes_ask
    return parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_bid:.4f}", "100"]],
                "no_dollars": [[f"{no_bid:.4f}", "100"]],
            }
        },
        timestamp=NOW,
    )


def forecast(p_up: float, *, confidence: float = 0.75, agreement: float = 0.80):
    return ProbabilityEstimate(
        p_up=p_up,
        p_down=1 - p_up,
        confidence=confidence,
        signal_agreement=agreement,
        component_probabilities={"terminal": p_up},
        regime=Regime.TREND_UP,
        raw_p_up=p_up,
    )


def test_favorable_poll_band_allows_aligned_trade():
    poll = market_poll_snapshot(book(0.87))
    failure = evaluate_poll_alignment(
        selected_side=ContractSide.YES,
        forecast=forecast(0.90),
        poll=poll,
        cfg=CFG,
    )
    assert failure is None


def test_low_poll_blocks_without_strong_evidence():
    poll = market_poll_snapshot(book(0.55))
    failure = evaluate_poll_alignment(
        selected_side=ContractSide.YES,
        forecast=forecast(0.60, confidence=0.60, agreement=0.60),
        poll=poll,
        cfg=CFG,
    )
    assert failure is not None
    assert failure.gate == "low_poll"


def test_low_poll_allows_with_strong_evidence():
    poll = market_poll_snapshot(book(0.55))
    failure = evaluate_poll_alignment(
        selected_side=ContractSide.YES,
        forecast=forecast(0.75, confidence=0.70, agreement=0.70),
        poll=poll,
        cfg=CFG,
    )
    assert failure is None


def test_contrarian_against_strong_poll_requires_counter_evidence():
    poll = market_poll_snapshot(book(0.88))
    failure = evaluate_poll_alignment(
        selected_side=ContractSide.NO,
        forecast=forecast(0.20, confidence=0.55, agreement=0.55),
        poll=poll,
        cfg=CFG,
    )
    assert failure is not None
    assert failure.gate == "poll_contrarian"


def test_poll_confirmation_blocks_mismatched_high_poll():
    poll = market_poll_snapshot(book(0.80))
    failure = evaluate_poll_confirmation(
        selected_side=ContractSide.YES,
        forecast=forecast(0.55),
        poll=poll,
        threshold=0.75,
    )
    assert failure is not None
    assert failure.gate == "poll_confirm"


def test_poll_confirmation_allows_aligned_high_poll():
    poll = market_poll_snapshot(book(0.80))
    failure = evaluate_poll_confirmation(
        selected_side=ContractSide.YES,
        forecast=forecast(0.78),
        poll=poll,
        threshold=0.75,
    )
    assert failure is None


def test_poll_confirmation_allows_longshot_low_poll():
    poll = market_poll_snapshot(book(0.35))
    failure = evaluate_poll_confirmation(
        selected_side=ContractSide.YES,
        forecast=forecast(0.52),
        poll=poll,
        threshold=0.75,
    )
    assert failure is None
