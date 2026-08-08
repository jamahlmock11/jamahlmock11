from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.backtest.backtester import (
    BacktestEvent,
    Backtester,
    ChronologyError,
    LookaheadError,
    Settlement,
)
from kalshi_bot.calibration.calibration import ProbabilityCalibrator
from kalshi_bot.domain import (
    ContractSide,
    DecisionAction,
    DecisionResult,
    Direction,
    MarketSnapshot,
)
from kalshi_bot.market.orderbook import parse_orderbook_fp

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def market(book_time=NOW):
    book = parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.4800", "10"]],
                "no_dollars": [["0.4800", "10"]],
            }
        },
        timestamp=book_time,
    )
    return MarketSnapshot(
        ticker="KXBTC15M-TEST",
        status="active",
        rules="CF Benchmarks BRTI",
        strike=65000,
        expiration=NOW + timedelta(minutes=10),
        open_time=NOW - timedelta(minutes=5),
        reference="BRTI",
        orderbook=book,
    )


def decision(action, *, edge=None):
    return DecisionResult(
        action=action,
        reason="replay",
        gate_failures=(),
        current_direction=Direction.FLAT,
        predicted_direction=Direction.UP,
        trade_direction=Direction.UP if action is DecisionAction.BUY_UP else Direction.FLAT,
        selected_side=ContractSide.YES,
        predicted_probability=0.78,
        executable_cost=0.52 if edge is not None else None,
        edge=edge,
        quantity=1,
    )


def test_calibration_fit_excludes_future_outcome_and_roundtrips(tmp_path):
    calibrator = ProbabilityCalibrator(n_bins=5)
    calibrator.add_sample(0.75, 1, NOW - timedelta(minutes=10), NOW - timedelta(minutes=5))
    calibrator.add_sample(0.75, 0, NOW - timedelta(minutes=2), NOW + timedelta(minutes=1))
    calibrator.fit(NOW)
    assert calibrator.fitted_sample_count == 1
    assert calibrator(0.75) == pytest.approx(2 / 3)
    path = tmp_path / "calibration.json"
    calibrator.save(path)
    loaded = ProbabilityCalibrator.load(path)
    assert loaded.fitted_sample_count == 1
    assert loaded(0.75) == pytest.approx(2 / 3)


def test_backtester_journals_trade_and_no_trade_with_settlement():
    rows = [
        BacktestEvent(
            decision_time=NOW,
            market=market(),
            decision=decision(DecisionAction.BUY_UP, edge=0.26),
        ),
        BacktestEvent(
            decision_time=NOW + timedelta(seconds=10),
            market=market(NOW + timedelta(seconds=10)),
            decision=decision(DecisionAction.HOLD),
        ),
    ]
    result = Backtester().run(
        rows,
        settlements={
            "KXBTC15M-TEST": Settlement(
                ContractSide.YES,
                NOW + timedelta(minutes=10),
            )
        },
    )
    assert len(result.decisions) == 2
    assert result.decisions[1].status == "hold"
    assert result.metrics.overall.trade_count == 1
    assert result.metrics.overall.total_pnl > 0


def test_backtester_rejects_lookahead_and_unsorted_rows():
    future_book = market(NOW + timedelta(seconds=1))
    with pytest.raises(LookaheadError):
        Backtester().run(
            [
                BacktestEvent(
                    decision_time=NOW,
                    market=future_book,
                    decision=decision(DecisionAction.NO_TRADE),
                )
            ]
        )
    with pytest.raises(ChronologyError):
        Backtester().run(
            [
                BacktestEvent(
                    decision_time=NOW + timedelta(seconds=1),
                    decision=decision(DecisionAction.NO_TRADE),
                ),
                BacktestEvent(
                    decision_time=NOW,
                    decision=decision(DecisionAction.NO_TRADE),
                ),
            ]
        )
