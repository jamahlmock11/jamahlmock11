from kalshi_btc_edge.config import RiskConfig
from kalshi_btc_edge.execution.risk import size_trade
from kalshi_btc_edge.models import Confidence, EdgeSignal, Side


def _sig(conf: Confidence = Confidence.HIGH, edge: float = 16.0) -> EdgeSignal:
    return EdgeSignal(
        market_ticker="KXBTCD-X",
        series="KXBTCD",
        kalshi_mid=0.22,
        options_prob_yes=0.378,
        edge_pp=edge,
        confidence=conf,
        side=Side.YES,
        strike_btc=65000,
        btc_spot=70000,
        iv_used=0.65,
        spread_cents=2.0,
        reason="test",
    )


def test_sizes_high_confidence():
    intent = size_trade(_sig(), RiskConfig(bankroll_usd=1000, kelly_fraction=0.25))
    assert intent is not None
    assert intent.contracts > 0
    assert intent.paper is True


def test_pass_not_sized():
    assert size_trade(_sig(Confidence.PASS, 3.0), RiskConfig()) is None
