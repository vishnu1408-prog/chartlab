from app import strategy as s


def leg(k, t, side, px, lots=1, lot=65, iv=0.15):
    return {"strike": k, "type": t, "side": side, "price": px, "lots": lots, "lot": lot, "iv": iv}


def test_bull_call_spread_limits_and_breakeven():
    legs = [leg(23000, "CE", "BUY", 150), leg(23200, "CE", "SELL", 60)]
    m = s.metrics(legs, 23000, 0.15, 5 / 365)
    assert m["max_loss"] == -90 * 65 and m["max_profit"] == (200 - 90) * 65
    assert m["breakevens"] == [23090] and m["net_premium"] == -90 * 65 and not m["unlimited_up"]
    mg = s.margin(legs, 23000, "NIFTY 50", "NFO", False, m)
    assert mg["margin"] == 0                                   # debit spread: nothing sold uncovered, premium paid already


def test_short_straddle_unlimited_two_breakevens_span_margin():
    legs = [leg(23000, "CE", "SELL", 150), leg(23000, "PE", "SELL", 140)]
    m = s.metrics(legs, 23000, 0.15, 5 / 365)
    assert m["max_profit"] == 290 * 65 and m["max_loss"] is None
    assert m["breakevens"] == [22710, 23290]
    mg = s.margin(legs, 23000, "NIFTY 50", "NFO", False, m)
    assert abs(mg["margin"] - 0.12 * 23000 * 65) < 1 and "NOT limited" in mg["basis"]


def test_iron_condor_margin_is_its_max_loss_and_pop_between_0_and_1():
    legs = [leg(22800, "PE", "BUY", 20), leg(22900, "PE", "SELL", 40), leg(23100, "CE", "SELL", 45), leg(23200, "CE", "BUY", 22)]
    m = s.metrics(legs, 23000, 0.15, 5 / 365)
    credit = 40 - 20 + 45 - 22
    assert m["max_profit"] == credit * 65 and m["max_loss"] == -(100 - credit) * 65
    assert s.margin(legs, 23000, "NIFTY 50", "NFO", False, m)["margin"] == 100 * 65
    assert 0.15 < m["pop"] < 0.5 and len(m["breakevens"]) == 2


def test_today_curve_meets_expiry_at_expiry_and_long_straddle_has_positive_vega():
    legs = [leg(23000, "CE", "BUY", 150), leg(23000, "PE", "BUY", 140)]
    assert abs(s.today_pnl(legs, 23300, 1e-7) - s.expiry_pnl(legs, 23300)) < 1e-6
    g = s.greeks(legs, 23000, 5 / 365)
    assert g["vega"] > 0 and g["theta"] < 0 and abs(g["delta"]) < 10
    c = s.curves(legs, 23000, 5 / 365, 0.15)
    assert 23000 in c["x"] and len(c["x"]) == len(c["expiry"]) == len(c["today"])
