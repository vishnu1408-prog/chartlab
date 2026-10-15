"""MCX commodities (Angel One): registry, 09:00-anchored bars, history files, resumable backfill, near-month token."""
import datetime as dt
import json
import time

import numpy as np
import pandas as pd
import pytest

from app import config, sources, store, symbols, sync


def mcx_minutes(days=3, start="2025-03-03", base=88000.0):
    rng = np.random.default_rng(3)
    out = []
    for d in pd.bdate_range(start, periods=days):
        idx = pd.date_range(d + pd.Timedelta(hours=9), d + pd.Timedelta(hours=23, minutes=29), freq="1min")
        c = base + np.cumsum(rng.normal(0, 5, len(idx)))
        out.append(pd.DataFrame({"open": c, "high": c + 3, "low": c - 3, "close": c, "volume": 1.0}, index=idx))
    df = pd.concat(out)
    df.index.name = "date"
    return df


@pytest.fixture
def gold(monkeypatch):
    d = symbols.mcx_dir("MCX GOLD")
    d.mkdir(parents=True, exist_ok=True)
    mcx_minutes().to_parquet(d / "1m_2025.parquet")
    symbols.all_symbols.cache_clear()
    yield "MCX GOLD"
    for f in d.glob("*"):
        f.unlink()
    d.rmdir()
    for p in [store._base_path("MCX GOLD")] + [store.ext_path("MCX GOLD", r) for r in store.EXT_RES]:
        p.unlink(missing_ok=True)
    symbols.all_symbols.cache_clear()
    store._cache.clear(), store._tf_cache.clear()


def test_listed_only_with_data_or_creds(gold):
    s = symbols.all_symbols()
    assert "MCX GOLD" in s and "MCX SILVER" not in s          # offline, no creds: only symbols with downloaded history
    g = s["MCX GOLD"]
    assert (g.exchange, g.session_start, g.square_off, g.lot) == ("MCX", 540, 1380, 10)


def test_bars_cover_evening_and_anchor_at_0900(gold):
    m = store.minute_bars(gold)
    assert m.index.max().strftime("%H:%M") == "23:29"         # evening session kept (not cut at 15:30)
    h = store.bars(gold, "60")
    assert h.index[0].strftime("%H:%M") == "09:00" and len(h) == 3 * 15
    assert len(store.bars(gold, "D")) == 3



def test_market_hours_mcx_direct(gold):
    assert sync.market_open(dt.datetime(2026, 10, 12, 22, 0), sym=gold)          # Monday night
    assert sync.market_open(dt.datetime(2026, 10, 12, 9, 0), sym=gold)
    assert not sync.market_open(dt.datetime(2026, 10, 12, 8, 30), sym=gold)
    assert not sync.market_open(dt.datetime(2026, 10, 10, 12, 0), sym=gold)     # Saturday


def test_backfill_is_chunked_resumable_and_splits_years(gold, monkeypatch):
    calls = []
    src = pd.concat([mcx_minutes(2, "2015-12-30"), mcx_minutes(2, "2016-01-04")])

    def fake(sym, a, b, interval="ONE_MINUTE", strict=False):
        calls.append((interval, a, b))
        if interval != "ONE_MINUTE":
            return pd.DataFrame()
        return src[(src.index >= a) & (src.index <= b)]
    monkeypatch.setattr(sources.Angel, "candles", fake)
    monkeypatch.setattr(sync, "HIST", (("1", "ONE_MINUTE", "2015-12-28", 3),))
    monkeypatch.setattr(sync, "now_ist", lambda: dt.datetime(2016, 1, 6, 0, 0))
    sync.backfill(gold)
    d = symbols.mcx_dir(gold)
    assert sorted(f.name for f in d.glob("1m_201*.parquet")) == ["1m_2015.parquet", "1m_2016.parquet"]
    assert len(pd.read_parquet(d / "1m_2016.parquet")) == len(src[src.index.year == 2016])
    assert all((b - a) <= pd.Timedelta(days=3) for _, a, b in calls)
    n = len(calls)
    sync.backfill(gold)                                       # progress saved → nothing fetched again
    assert len(calls) == n
    assert json.loads((d / "progress.json").read_text())["1"].startswith("2015-12-28")


def test_token_picks_nearest_live_contract(monkeypatch):
    rows = [{"exch_seg": "MCX", "instrumenttype": "FUTCOM", "name": "GOLD", "symbol": f"GOLD{e}FUT", "expiry": e, "token": t}
            for e, t in (("05OCT2026", "1"), ("04DEC2026", "2"), ("05FEB2027", "3"))]
    p = config.DATA_DIR / "cache" / "angel_scrip_master.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows))
    monkeypatch.setattr(sources.Angel, "_tokens", None)

    class D(dt.date):
        @classmethod
        def today(cls):
            return dt.date(2026, 10, 12)
    monkeypatch.setattr(sources.dt, "date", D)
    monkeypatch.setattr(symbols, "get", lambda s: symbols.MCX[s])
    assert sources.Angel.token("MCX GOLD") == "2"            # Oct expired → Dec, not the later Feb contract
    p.unlink()
    monkeypatch.setattr(sources.Angel, "_tokens", None)


def test_angel_request_windows_stay_under_8000_bars():
    for (ex, iv), days in sources.Angel.STEP_DAYS.items():
        per_day = {"ONE_MINUTE": 375 if ex == "NSE" else 870, "FIVE_MINUTE": 75 if ex == "NSE" else 174, "ONE_DAY": 1}[iv]
        assert days * 5 / 7 * per_day < 8000 or iv == "ONE_DAY" and days < 8000


def test_index_token_matches_name_or_symbol(monkeypatch):
    rows = [{"exch_seg": "NSE", "instrumenttype": "AMXIDX", "name": "NIFTY", "symbol": "Nifty 50", "token": "99926000"},
            {"exch_seg": "NSE", "instrumenttype": "AMXIDX", "name": "BANKNIFTY", "symbol": "Nifty Bank", "token": "99926009"},
            {"exch_seg": "BSE", "instrumenttype": "AMXIDX", "name": "SENSEX", "symbol": "SENSEX", "token": "99919000"}]
    p = config.DATA_DIR / "cache" / "angel_scrip_master.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows))
    monkeypatch.setattr(sources.Angel, "_tokens", None)
    assert sources.Angel.token("NIFTY 50") == "99926000"            # Angel's name is "NIFTY", its symbol "Nifty 50"
    assert sources.Angel.token("NIFTY BANK") == "99926009"
    p.unlink()
    monkeypatch.setattr(sources.Angel, "_tokens", None)


def test_live_tick_folds_quotes_into_minute_bars():
    import datetime as dt
    from app import store, sync
    t = dt.datetime(2026, 10, 13, 10, 0, 5)
    b = sync._tick(store.empty(), t, 100.0)
    b = sync._tick(b, t + dt.timedelta(seconds=20), 103.0, 5)
    b = sync._tick(b, t + dt.timedelta(seconds=40), 99.0)
    assert len(b) == 1 and list(b.iloc[0][["open", "high", "low", "close", "volume"]]) == [100, 103, 99, 99, 5]
    b = sync._tick(b, t + dt.timedelta(minutes=1), 101.0)
    assert len(b) == 2 and b.index[-1] == dt.datetime(2026, 10, 13, 10, 1) and b.iloc[-1]["open"] == 101


def test_history_download_stands_aside_in_nse_hours():
    import datetime as dt
    from app import sync
    assert sync.nse_busy(dt.datetime(2026, 10, 13, 10, 0))
    assert not sync.nse_busy(dt.datetime(2026, 10, 13, 16, 0))
    assert not sync.nse_busy(dt.datetime(2026, 10, 17, 10, 0))     # Saturday
