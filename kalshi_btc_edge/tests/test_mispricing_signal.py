from datetime import datetime, timedelta, timezone

from kalshi_btc_edge.config import AppConfig, ConfidenceConfig
from kalshi_btc_edge.models import BookQuote, Confidence, KalshiMarket, SmilePoint, VolSmile
from kalshi_btc_edge.strategies.mispricing import signal_for_market


def test_detects_undervalued_yes():
    # Multi-day tenor so short-tenor dampening does not collapse IV;
    # strike slightly OTM so options P(YES) lands near the 37.8% example.
    close = datetime.now(timezone.utc) + timedelta(days=7)
    market = KalshiMarket(
        ticker="KXBTCD-TEST-T65000",
        event_ticker="KXBTCD-TEST",
        series_ticker="KXBTCD",
        title="BTC above 65k?",
        status="active",
        close_time=close,
        floor_strike=67000.0,
        strike_type="greater",
        book=BookQuote(yes_bid=0.20, yes_ask=0.24),  # mid 22%
    )
    smile = VolSmile(
        underlying="IBIT",
        spot=36.5,
        points=[SmilePoint(1.0, 0.65), SmilePoint(0.95, 0.68), SmilePoint(1.05, 0.63)],
    )
    cfg = AppConfig(confidence=ConfidenceConfig())
    sig = signal_for_market(market, btc_spot=65000.0, ibit_spot=36.5, smile=smile, cfg=cfg)
    assert sig is not None
    assert sig.options_prob_yes > sig.kalshi_mid
    assert 5.0 <= abs(sig.edge_pp) <= 25.0
    assert sig.confidence in {Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW}
