from kalshi_btc_edge.pricing.black_scholes import digital_call_prob, digital_call_prob_ge


def test_atm_digital_near_half():
    p = digital_call_prob(spot=100, strike=100, t_years=1 / 365, iv=0.65, r=0.0, q=0.0)
    assert 0.45 < p < 0.55


def test_deep_itm_near_one():
    p = digital_call_prob(spot=100, strike=50, t_years=1 / 24 / 365, iv=0.65)
    assert p > 0.95


def test_deep_otm_near_zero():
    p = digital_call_prob(spot=100, strike=200, t_years=1 / 24 / 365, iv=0.65)
    assert p < 0.05


def test_expired_ge():
    assert digital_call_prob_ge(100, 100, 0, 0.65) == 1.0
    assert digital_call_prob_ge(99, 100, 0, 0.65) == 0.0
