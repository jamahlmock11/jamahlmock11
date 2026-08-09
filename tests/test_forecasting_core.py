from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kalshi_bot.data.cf_benchmark import (
    BenchmarkSourceError,
    StaleBenchmarkError,
    parse_brti_payload,
)
from kalshi_bot.data.supporting_feeds import ConstituentBRTIProxy
from kalshi_bot.domain import (
    BenchmarkQuote,
    ContractSide,
    DecisionAction,
    FeatureSnapshot,
    MarketPosition,
    MarketSnapshot,
    ProbabilityEstimate,
    Regime,
    SupportingAggregate,
    SupportingQuote,
    TrajectoryState,
)
from kalshi_bot.features.engine import FeatureEngine, FeatureEngineConfig, classify_trajectory
from kalshi_bot.market.discovery import discover_current_market
from kalshi_bot.market.orderbook import parse_orderbook_fp
from kalshi_bot.models.ensemble import EnsembleProbabilityModel
from kalshi_bot.strategies.decision import DecisionConfig, DecisionEngine

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def book(yes_ask: float = 0.52):
    no_bid = 1.0 - yes_ask
    yes_bid = yes_ask - 0.02
    return parse_orderbook_fp(
        {
            "orderbook_fp": {
                "yes_dollars": [[f"{yes_bid:.4f}", "100"]],
                "no_dollars": [[f"{no_bid:.4f}", "100"]],
            }
        },
        timestamp=NOW,
    )


def market(yes_ask: float = 0.52, position: MarketPosition | None = None):
    return MarketSnapshot(
        ticker="KXBTC15M-26AUG080815-00",
        status="active",
        rules="If the 60 second average of CF Benchmarks' BRTI is at least the target",
        strike=65_000,
        expiration=NOW + timedelta(minutes=10),
        open_time=NOW - timedelta(minutes=5),
        reference="CME CF Bitcoin Real Time Index (BRTI)",
        orderbook=book(yes_ask),
        current_position=position,
    )


def features(*, trajectory=TrajectoryState.ACCELERATING_UP):
    return FeatureSnapshot(
        timestamp=NOW,
        current_price=65_020,
        strike=65_000,
        seconds_remaining=600,
        changes={5: 0.0002, 10: 0.0003, 15: 0.0004, 30: 0.0005, 60: 0.0006, 120: 0.0007},
        velocities={5: 0.00004, 10: 0.00003, 15: 0.000026},
        acceleration=0.000001,
        short_trend=0.0003,
        medium_trend=0.0006,
        realized_vol=0.65,
        expected_remaining_move=80,
        z_distance_to_strike=0.25,
        mean_reversion_score=-0.1,
        orderbook_imbalance=0.1,
        cross_venue_agreement=0.9,
        cross_venue_dispersion=0.0002,
        data_completeness=1.0,
        trajectory=trajectory,
        sample_count=301,
        oldest_sample_age=300,
    )


def forecast(p_up: float):
    return ProbabilityEstimate(
        p_up=p_up,
        p_down=1 - p_up,
        confidence=0.9,
        signal_agreement=0.8,
        component_probabilities={"terminal_distribution": p_up},
        regime=Regime.TREND_UP if p_up >= 0.5 else Regime.TREND_DOWN,
        raw_p_up=p_up,
    )


def benchmark(timestamp: datetime = NOW):
    return BenchmarkQuote(
        price=65_020,
        timestamp=timestamp,
        source="CME CF Bitcoin Real Time Index (BRTI)",
    )


def test_brti_parser_requires_explicit_fresh_provenance():
    quote = parse_brti_payload(
        {"data": {"symbol": "BRTI", "price": "65020.1", "timestamp": NOW.isoformat()}},
        now=NOW,
        max_age=timedelta(seconds=15),
    )
    assert quote.primary and quote.price == pytest.approx(65020.1)
    with pytest.raises(BenchmarkSourceError):
        parse_brti_payload(
            {"source": "Coinbase", "price": 65020, "timestamp": NOW.isoformat()},
            now=NOW,
            max_age=timedelta(seconds=15),
        )
    with pytest.raises(StaleBenchmarkError):
        parse_brti_payload(
            {"symbol": "BRTI", "price": 65020, "timestamp": (NOW - timedelta(seconds=16)).isoformat()},
            now=NOW,
            max_age=timedelta(seconds=15),
        )


def test_kalshi_cfbenchmarks_values_parser():
    from kalshi_bot.data.cf_benchmark import parse_kalshi_cfbenchmarks_values_payload

    payload = {
        "serverTime": NOW.isoformat(),
        "payload": [
            {"value": "65020.1", "time": int((NOW - timedelta(seconds=2)).timestamp() * 1000)},
            {"value": "65021.5", "time": int((NOW - timedelta(seconds=1)).timestamp() * 1000)},
        ],
    }
    quote = parse_kalshi_cfbenchmarks_values_payload(
        payload,
        now=NOW,
        max_age=timedelta(seconds=15),
    )
    assert quote.primary and quote.price == pytest.approx(65021.5)


def test_constituent_proxy_is_robust_and_never_primary():
    class StubFeeds:
        def get_quotes(self, *, now=None):
            return (
                SupportingQuote(65000, NOW, "Coinbase", bid=64999, ask=65001),
                SupportingQuote(65002, NOW, "Kraken", bid=65001, ask=65003),
                SupportingQuote(64998, NOW, "Bitstamp", bid=64997, ask=64999),
            )

    proxy = ConstituentBRTIProxy(StubFeeds(), minimum_venues=3)
    quote = proxy.get_quote(now=NOW)
    assert quote.price == pytest.approx(65000)
    assert quote.is_proxy
    assert not quote.primary
    assert quote.constituent_count == 3
    assert quote.dispersion < 0.001
    FeatureEngine(FeatureEngineConfig(allow_proxy=True)).add_quote(quote)
    with pytest.raises(ValueError):
        FeatureEngine().add_quote(quote)


def test_discovery_selects_active_explicit_brti_contract():
    raw = {
        "ticker": "KXBTC15M-26AUG080815-00",
        "status": "active",
        "rules_primary": "60 seconds of CF Benchmarks' BRTI determine settlement",
        "floor_strike": 65000,
        "open_time": (NOW - timedelta(minutes=5)).isoformat(),
        "close_time": (NOW + timedelta(minutes=10)).isoformat(),
    }
    result = discover_current_market(
        [raw],
        orderbooks={raw["ticker"]: book()},
        now=NOW,
    )
    assert result.market is not None
    assert result.market.strike == 65000

    malformed = {**raw, "floor_strike": None, "rules_primary": "Coinbase spot"}
    rejected = discover_current_market(
        [malformed],
        orderbooks={raw["ticker"]: book()},
        now=NOW,
    )
    assert rejected.market is None
    assert rejected.rejections


def test_discovery_accepts_live_kalshi_orderbook_envelope():
    raw = {
        "ticker": "KXBTC15M-26AUG080815-00",
        "status": "active",
        "rules_primary": "CF Benchmarks' BRTI determines settlement",
        "floor_strike": 65000,
        "open_time": (NOW - timedelta(minutes=5)).isoformat(),
        "close_time": (NOW + timedelta(minutes=10)).isoformat(),
    }
    payload = {
        "orderbook_fp": {
            "yes_dollars": [["0.4800", "10"]],
            "no_dollars": [["0.5000", "10"]],
        }
    }
    result = discover_current_market(
        [raw],
        orderbooks={raw["ticker"]: payload},
        now=NOW,
    )
    assert result.market is not None
    assert result.market.yes_ask == pytest.approx(0.50)


def test_feature_engine_is_causal_and_detects_reversal():
    engine = FeatureEngine()
    for seconds_ago in range(300, -1, -1):
        timestamp = NOW - timedelta(seconds=seconds_ago)
        # Fall for most of the window, then recover sharply in the final 15s.
        price = 65_100 - (300 - seconds_ago) * 0.3
        if seconds_ago <= 15:
            price += (15 - seconds_ago) * (10 / 15)
        engine.add_quote(
            BenchmarkQuote(
                price=price,
                timestamp=timestamp,
                source="BRTI",
                primary=True,
                is_live=False,
                replay=True,
            )
        )
    # Future data is ignored by the causal build.
    engine.add_quote(
        BenchmarkQuote(
            price=70_000,
            timestamp=NOW + timedelta(seconds=1),
            source="BRTI",
            primary=True,
            is_live=False,
            replay=True,
        )
    )
    supporting = SupportingAggregate(
        price=65_050,
        timestamp=NOW,
        quotes=(
            SupportingQuote(65_050, NOW, "Coinbase"),
            SupportingQuote(65_052, NOW, "Kraken"),
        ),
        dispersion=0.0001,
        healthy_venues=2,
        required_venues=2,
    )
    snapshot = engine.compute(market(), now=NOW, supporting=supporting)
    assert snapshot.current_price < 70_000
    assert snapshot.trajectory is TrajectoryState.REVERSING_UP
    assert snapshot.data_completeness == 1.0
    assert classify_trajectory(0.001, -0.001, 0.0001) is TrajectoryState.REVERSING_UP


def test_ensemble_outputs_complementary_bounded_probabilities():
    estimate = EnsembleProbabilityModel().estimate(
        features(),
        Regime.TREND_UP,
        options_volatility=0.7,
        market_prior=0.55,
        historical_prior=0.58,
    )
    assert 0.03 <= estimate.p_up <= 0.97
    assert estimate.p_up + estimate.p_down == pytest.approx(1.0)
    assert 0 <= estimate.confidence <= 1


def test_final_minute_uses_locked_brti_average():
    engine = FeatureEngine()
    for seconds_ago in range(300, -1, -1):
        engine.add_quote(
            BenchmarkQuote(
                price=65_100,
                timestamp=NOW - timedelta(seconds=seconds_ago),
                source="BRTI",
                is_live=False,
                replay=True,
            )
        )
    late_market = replace(market(), expiration=NOW + timedelta(seconds=30))
    snapshot = engine.compute(late_market, now=NOW)
    assert snapshot.settlement_locked_fraction == pytest.approx(0.5)
    assert snapshot.settlement_effective_strike < snapshot.strike


@pytest.mark.parametrize(
    ("probability", "yes_ask", "expected"),
    [
        (0.70, 0.51, DecisionAction.NO_TRADE),
        (0.72, 0.52, DecisionAction.BUY_UP),
        (0.78, 0.52, DecisionAction.BUY_UP),
    ],
)
def test_hard_edge_boundary_up(probability, yes_ask, expected):
    result = DecisionEngine(DecisionConfig()).decide(
        market(yes_ask),
        forecast(probability),
        features(),
        benchmark(),
        now=NOW,
    )
    assert result.action is expected


def test_buy_down_uses_executable_no_price():
    # YES ask .50 implies NO bid .50; YES bid .48 implies executable NO ask .52.
    result = DecisionEngine().decide(
        market(0.50),
        forecast(0.22),
        replace(features(), trajectory=TrajectoryState.ACCELERATING_DOWN),
        benchmark(),
        now=NOW,
    )
    assert result.action is DecisionAction.BUY_DOWN
    assert result.edge == pytest.approx(0.26)


def test_proxy_requires_extra_edge_and_is_never_allowed_by_default():
    proxy = BenchmarkQuote(
        price=65020,
        timestamp=NOW,
        source="Unofficial CME CF BRTI constituent proxy",
        primary=False,
        is_proxy=True,
        constituent_count=3,
        dispersion=0.0002,
    )
    allowed = DecisionEngine(
        DecisionConfig(allow_proxy_data=True)
    ).decide(
        market(0.52),
        forecast(0.78),
        features(),
        proxy,
        now=NOW,
    )
    assert allowed.action is DecisionAction.BUY_UP

    insufficient_proxy_edge = DecisionEngine(
        DecisionConfig(allow_proxy_data=True)
    ).decide(
        market(0.52),
        forecast(0.76),
        features(),
        proxy,
        now=NOW,
    )
    assert insufficient_proxy_edge.action is DecisionAction.NO_TRADE

    live_locked = DecisionEngine().decide(
        market(0.52),
        forecast(0.78),
        features(),
        proxy,
        now=NOW,
    )
    assert live_locked.action is DecisionAction.NO_TRADE


def test_existing_position_exits_before_opposite_entry():
    held = MarketPosition(ContractSide.YES, quantity=1, average_price=0.4)
    result = DecisionEngine().decide(
        market(0.52, held),
        forecast(0.2),
        replace(features(), trajectory=TrajectoryState.REVERSING_DOWN),
        benchmark(),
        now=NOW,
    )
    assert result.action is DecisionAction.EXIT
    assert result.selected_side is ContractSide.YES
