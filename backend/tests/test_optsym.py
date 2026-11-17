import datetime as dt

import pandas as pd

from app import config, optsym, opthist, store, symbols


def test_option_names_parse_and_build():
    n = optsym.name("NIFTY", "2031-01-07", 23100, "CE")
    assert n == "NIFTY 07-Jan-31 23100 CE"
    p = optsym.parse(n)
    assert p["root"] == "NIFTY" and p["expiry"] == dt.date(2031, 1, 7) and p["strike"] == 23100 and p["seg"] == "NFO"
    assert optsym.parse("CRUDEOIL 17-Nov-26 9250.5 PE")["seg"] == "MCX"
    assert optsym.parse("RELIANCE 27-Oct-26 1210 CE")["underlying"] == "RELIANCE"
    assert optsym.parse("NIFTY 50") is None


def test_option_symbol_charts_from_saved_files():
    e = dt.date(2031, 1, 7)
    rows = pd.DataFrame({"timestamp": pd.to_datetime(["2031-01-06 09:15", "2031-01-06 09:16", "2031-01-06 09:16"]),
                         "open": [100.0, 101.0, 50.0], "high": [102.0, 103.0, 51.0], "low": [99.0, 100.0, 49.0],
                         "close": [101.0, 102.0, 50.5], "volume": [5, 6, 7], "strike": [23100.0, 23100.0, 23200.0],
                         "option_type": ["CE", "CE", "CE"]})
    opthist.merge(e, rows)
    sym = optsym.name("NIFTY", e, 23100, "CE")
    s = symbols.get(sym)
    assert s.kind == "option" and s.exchange == "NFO"
    m = store.minute_bars(sym)
    assert list(m.close) == [101.0, 102.0]
    (config.HF_DIR / "options" / "NIFTY" / f"{e}.parquet").unlink()
