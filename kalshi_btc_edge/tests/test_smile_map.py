from pathlib import Path

from kalshi_btc_edge.config import PricingConfig
from kalshi_btc_edge.pricing.smile import load_smile, map_btc_strike_to_ibit


def test_map_btc_to_ibit():
    # BTC 100k, IBIT 50 → strike 110k maps to IBIT 55
    assert abs(map_btc_strike_to_ibit(110_000, 100_000, 50.0) - 55.0) < 1e-9


def test_load_file_smile():
    root = Path(__file__).resolve().parents[1]
    smile = load_smile(PricingConfig(smile_source="file"), root)
    assert len(smile.points) >= 3
    atm = smile.iv_at_moneyness(1.0)
    assert 0.5 <= atm <= 0.8
