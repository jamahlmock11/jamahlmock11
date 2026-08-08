from kalshi_btc_edge.config import ConfidenceConfig
from kalshi_btc_edge.models import Confidence
from kalshi_btc_edge.pricing.edge import classify_confidence, compute_edge_pp


def test_example_15_8pp_edge():
    # Kalshi 22% vs options 37.8% → 15.8pp
    edge = compute_edge_pp(0.378, 0.22)
    assert abs(edge - 15.8) < 1e-9
    conf = classify_confidence(edge, spread_cents=2.0, cfg=ConfidenceConfig())
    assert conf == Confidence.HIGH


def test_high_demoted_on_wide_spread():
    conf = classify_confidence(16.0, spread_cents=10.0, cfg=ConfidenceConfig())
    assert conf == Confidence.MEDIUM


def test_medium_and_low_and_pass():
    cfg = ConfidenceConfig()
    assert classify_confidence(12.0, 3.0, cfg) == Confidence.MEDIUM
    assert classify_confidence(7.0, 3.0, cfg) == Confidence.LOW
    assert classify_confidence(3.0, 1.0, cfg) == Confidence.PASS


def test_absurd_edge_is_pass():
    cfg = ConfidenceConfig(max_credible_edge_pp=25.0)
    assert classify_confidence(33.0, 1.0, cfg) == Confidence.PASS
