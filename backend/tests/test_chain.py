"""Option chain: strike window, lots, ΔOI since first seen, PCR / straddle, IV round-trip (no network)."""
import datetime as dt

import pytest

from app import chain
from app.options import bs


def test_pick_strikes_centres_on_spot():
    ks = [22000 + 50 * i for i in range(40)]
    assert chain.pick_strikes(ks, 22468, 2) == [22350, 22400, 22450, 22500, 22550]
    assert chain.pick_strikes(ks, 21000, 2) == [22000, 22050, 22100]          # clipped at the edge
    assert chain.pick_strikes([], 100, 2) == []


@pytest.mark.parametrize("call", [True, False])
def test_implied_vol_round_trip(call):
    S, K, T = 22450, 22500, 3 / 365
    price = bs(S, K, T, 0.15, call)
    assert chain.implied_vol(price, S, K, T, call) == pytest.approx(15.0, abs=0.1)
    if not call:
        assert chain.implied_vol(49, S, K, T, call) is None                     # below the 50-point intrinsic value
    assert chain.implied_vol(None, S, K, T, call) is None


def test_years_left_uses_exchange_close():
    now = dt.datetime(2026, 10, 12, 21, 30)
    assert chain.years_left(now, dt.date(2026, 10, 12), "MCX") * 365 * 24 == pytest.approx(2.0)
    assert chain.years_left(now, dt.date(2026, 10, 12), "NFO") > 0                       # never 0 / negative


def test_build_chain_table():
    chain._oi0.clear()
    exp = dt.date(2026, 10, 13)
    rows = [(exp, k, t, f"{k}{t}", f"NIFTY13OCT26{k}{t}", 65) for k in (22400, 22450, 22500) for t in ("CE", "PE")]
    rows.append((dt.date(2026, 10, 20), 22450, "CE", "x", "NIFTY20OCT2622450CE", 65))
    q = lambda ltp, oi: {"ltp": ltp, "netChange": 1.0, "opnInterest": oi, "tradeVolume": 6500,
                         "depth": {"buy": [{"price": ltp - 0.5}], "sell": [{"price": ltp + 0.5}]}}
    quotes = {"22400CE": q(90, 650), "22400PE": q(40, 1300), "22450CE": q(60, 1300), "22450PE": q(58, 650),
              "22500CE": q(35, 2600), "22500PE": q(85, 650)}
    now = dt.datetime(2026, 10, 12, 10, 0)
    r = chain.build("NIFTY 50", exp, rows, quotes, 22452.0, {"netChange": -20}, now, "NFO", "NIFTY 50", 1, [exp])
    assert [x["strike"] for x in r["rows"]] == [22400, 22450, 22500]
    assert r["atm"] == 22450 and r["straddle"] == 118 and r["lot"] == 65
    atm = r["rows"][1]
    assert atm["ce"]["oi"] == 20 and atm["ce"]["vol"] == 100 and atm["ce"]["bid"] == 59.5 and atm["ce"]["doi"] == 0
    assert r["pcr"] == round((20 + 10 + 10) / (10 + 20 + 40), 2)
    assert 5 < atm["ce"]["iv"] < 40
    quotes["22450CE"] = q(55, 1950)                                            # OI up 10 lots since 10:00
    r2 = chain.build("NIFTY 50", exp, rows, quotes, 22452.0, None, now.replace(minute=30), "NFO", "NIFTY 50", 1, [exp])
    assert r2["rows"][1]["ce"]["doi"] == 10 and r2["oi_since"] == "10:00"


def test_endpoint_rejects_unsupported(client):
    assert client.get("/api/chain", params={"symbol": "NIFTY IT"}).status_code in (404, 400)


def test_mcx_volume_is_already_in_lots():
    chain._oi0.clear()
    exp = dt.date(2026, 10, 15)
    rows = [(exp, 8850.0, t, t, f"CRUDEOIL15OCT268850{t}", 100) for t in ("CE", "PE")]
    quotes = {t: {"ltp": 200.0, "opnInterest": 240000, "tradeVolume": 206} for t in ("CE", "PE")}
    r = chain.build("MCX CRUDEOIL", exp, rows, quotes, 8864.0, None, dt.datetime(2026, 10, 12, 13, 0), "MCX", "CRUDEOIL19OCT26FUT", 1, [exp])
    assert r["rows"][0]["ce"]["oi"] == 2400 and r["rows"][0]["ce"]["vol"] == 206
