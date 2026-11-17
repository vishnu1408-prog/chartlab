import numpy as np

from app import oi


def test_max_pain_is_where_option_buyers_get_least():
    k = np.array([100.0, 110.0, 120.0])
    # heavy calls at 100 and heavy puts at 120 → buyers get least near 110
    assert oi.max_pain(k, np.array([10.0, 1, 1]), np.array([1.0, 1, 10])) == 110.0
    assert oi.max_pain(k, np.array([0.0, 0, 50]), np.array([0.0, 0, 0])) in (100.0, 110.0, 120.0)


def test_buildup_labels():
    assert oi.buildup(1.2, 5.0) == "Long buildup"
    assert oi.buildup(-1.2, 5.0) == "Short buildup"
    assert oi.buildup(1.2, -3.0) == "Short covering"
    assert oi.buildup(-1.2, -3.0) == "Long unwinding"
    assert oi.buildup(None, 2.0) == "—"


def _chain(spot, ce300=100):
    return {"symbol": "NIFTY 50", "expiry": "2026-10-27", "spot": spot, "lot": 65, "since": "yesterday's close",
            "rows": [{"strike": 22600.0, "ce": {"oi": 50, "chg": 5, "ltp": 120.0, "vol": 10, "iv": 14.0},
                      "pe": {"oi": 200, "chg": -3, "ltp": 60.0, "vol": 8, "iv": 15.0}},
                     {"strike": 22700.0, "ce": {"oi": ce300, "chg": 9, "ltp": 70.0, "vol": 20, "iv": 13.5}, "pe": None}],
            "pcr": 1.3, "max_pain": 22600.0, "call_wall": 22700.0, "put_wall": 22600.0, "atm": 22700.0, "atm_iv": 14.0,
            "ce_oi": 150.0, "pe_oi": 200.0}


def test_chains_are_saved_once_a_minute_and_rewind_returns_the_minute_at_or_before():
    import datetime as dt
    t = dt.datetime(2026, 10, 21, 10, 0, 12)
    assert oi.save_chain(_chain(22650.0), t)
    assert not oi.save_chain(_chain(22651.0), t.replace(second=40))           # same minute → skipped
    assert oi.save_chain(_chain(22690.0, ce300=180), t + dt.timedelta(minutes=5))
    old = oi.stored_chain("NIFTY 50", "2026-10-21T10:03")
    assert old["at"] == "10:00" and old["spot"] == 22650.0 and old["replay"]
    assert old["rows"][1]["ce"]["oi"] == 100 and old["rows"][1]["pe"] is None and old["rows"][0]["pe"]["iv"] == 15.0
    new = oi.stored_chain("NIFTY 50", "2026-10-21T11:00")
    assert new["at"] == "10:05" and new["rows"][1]["ce"]["oi"] == 180
    assert oi.stored_chain("NIFTY 50", "2026-10-21T09:20")["at"] == "10:00"     # before the first one → the first
    assert oi.times("NIFTY 50", "2026-10-21")["expiries"]["2026-10-27"] == ["10:00", "10:05"]
    assert [r["ts"][11:16] for r in oi.history("NIFTY 50", "2026-10-21")] == ["10:00", "10:05"]
    assert "2026-10-21" in oi.days("NIFTY 50")["recorded"]
    assert "error" in oi.stored_chain("NIFTY 50", "2026-10-22T10:00")


def test_screener_snapshots_rewind():
    import datetime as dt
    t = dt.datetime(2026, 10, 21, 10, 0)
    oi.save_screener([{"sym": "TCS", "ltp": 3000.0, "chg": 1.0, "buildup": "Long buildup"}], t)
    oi.save_screener([{"sym": "TCS", "ltp": 3010.0, "chg": 1.3, "buildup": "Long buildup"}], t + dt.timedelta(minutes=5))
    r = oi.stored_screener("2026-10-21T10:04")
    assert r["at"] == "10:00" and r["rows"][0]["ltp"] == 3000.0
    assert oi.screener_times("2026-10-21") == ["10:00", "10:05"] and oi.screener_days()[0] == "2026-10-21"
