import datetime as dt

import pandas as pd

from app import config, opthist, options


def _rows(ts, k, typ, px):
    return pd.DataFrame({"timestamp": pd.to_datetime(ts), "open": px, "high": px, "low": px, "close": px,
                         "volume": 10, "strike": float(k), "option_type": typ})


def test_merge_dedups_new_rows_win_and_load_chain_reads_it():
    e = dt.date(2031, 1, 7)
    opthist.merge(e, _rows(["2031-01-06 09:15", "2031-01-06 09:16"], 23000, "CE", [100.0, 101.0]))
    opthist.merge(e, _rows(["2031-01-06 09:16", "2031-01-06 09:17"], 23000, "CE", [111.0, 102.0]))
    ch = options.load_chain("NIFTY", e)
    c = ch[(ch.strike == 23000) & (ch.option_type == "CE")].set_index("date").close
    assert list(c) == [100.0, 111.0, 102.0]
    assert e in options._with_local("NIFTY", [])
    (config.HF_DIR / "options" / "NIFTY" / f"{e}.parquet").unlink()


def test_mcx_option_roots_sessions_and_expiry_rules():
    assert options.root_of("MCX CRUDEOIL") == "CRUDEOIL" and options.root_of("SENSEX") == "SENSEX"
    assert options.root_of("NIFTY 50") == "NIFTY"
    ex = options._rule_expiries("CRUDEOIL", dt.date(2026, 11, 1), dt.date(2026, 11, 30))
    assert dt.date(2026, 11, 17) in ex                       # Tue 17 Nov 2026
    ex = options._rule_expiries("GOLD", dt.date(2026, 11, 1), dt.date(2026, 11, 30))
    assert dt.date(2026, 11, 27) in ex                       # 28 Nov is a Saturday → Friday 27th
    # time to expiry runs to 23:30 for MCX: 20:00 on expiry day leaves 210 of 870 session minutes
    t = pd.Timestamp("2026-11-17 20:00")
    assert abs(options.years_to_expiry(t, dt.date(2026, 11, 17), "CRUDEOIL") * 252 - 210 / 870) < 1e-9
    assert options.years_to_expiry(t, dt.date(2026, 11, 17), "NIFTY") == 0          # NSE closed at 15:30
    assert options.dataset_expiries("CRUDEOIL") == []                                   # nothing saved, no HF call


def test_model_vol_mcx_uses_realised_vol_and_falls_back():
    v = options.model_vol("MCX CRUDEOIL", "CRUDEOIL", pd.Timestamp("2026-10-16 10:00"))
    assert 0.10 <= v <= 1.20
