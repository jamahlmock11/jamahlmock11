from datetime import datetime, timezone

from kalshi_btc_edge.clients.polymarket import PolyMarket
from kalshi_btc_edge.config import CrossVenueConfig
from kalshi_btc_edge.models import BookQuote, KalshiMarket
from kalshi_btc_edge.strategies.cross_venue import scan_cross_venue


def _km(ticker: str, yes_ask: float, close: datetime) -> KalshiMarket:
    return KalshiMarket(
        ticker=ticker,
        event_ticker="EVT",
        series_ticker="KXBTC15M",
        title="BTC up?",
        status="active",
        close_time=close,
        floor_strike=65000.0,
        strike_type="greater_or_equal",
        book=BookQuote(yes_bid=yes_ask - 0.01, yes_ask=yes_ask),
    )


def test_arb_when_sum_under_one():
    t = datetime(2026, 8, 8, 5, 0, tzinfo=timezone.utc)
    kalshi = [_km("KXBTC15M-TEST-45", yes_ask=0.42, close=t)]
    poly = [
        PolyMarket(
            id="poly1",
            question="Bitcoin up in 15 minutes?",
            end_time=t,
            yes_ask=0.55,
            no_ask=0.50,
        )
    ]
    # 0.42 + 0.50 = 0.92 < 1.00
    arbs = scan_cross_venue(kalshi, poly, CrossVenueConfig())
    assert any(a.combined_ask < 1.0 for a in arbs)
    assert any(a.kalshi_side == "YES" and a.poly_side == "NO" for a in arbs)


def test_no_arb_when_sum_above():
    t = datetime(2026, 8, 8, 5, 0, tzinfo=timezone.utc)
    # yes_bid=0.59, yes_ask=0.60 → no_ask≈0.41; keep both path sums ≥ 1.00
    kalshi = [_km("KXBTC15M-TEST-45", yes_ask=0.60, close=t)]
    poly = [
        PolyMarket(
            id="poly1",
            question="Bitcoin up?",
            end_time=t,
            yes_ask=0.70,
            no_ask=0.70,
        )
    ]
    arbs = scan_cross_venue(kalshi, poly, CrossVenueConfig())
    assert arbs == []
