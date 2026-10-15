import numpy as np
import pandas as pd
import pytest

from app import store, symbols
from conftest import synthetic_minutes


def test_epoch_treats_ist_wall_time_as_utc():
    t = store.to_epoch(pd.DatetimeIndex(["2024-01-01 09:15:00"]))[0]
    assert t % 86400 == 9 * 3600 + 15 * 60
    assert store.from_epoch(t) == pd.Timestamp("2024-01-01 09:15:00")


def test_symbols_registry_prefers_index_dataset():
    s = symbols.all_symbols()
    assert s["NIFTY 50"].kind == "index" and s["NIFTY 50"].lot == 65
    assert s["TESTSTOCK"].kind == "stock" and s["TESTSTOCK"].yahoo == "TESTSTOCK.NS"
    with pytest.raises(KeyError):
        symbols.get("NOPE")


@pytest.mark.parametrize("tf,n", [("5", 75), ("15", 25), ("30", 13), ("60", 7), ("D", 1)])
def test_resample_is_session_anchored(tf, n):
    df = synthetic_minutes(days=3)
    r = store.resample(df, tf)
    per_day = r.groupby(r.index.normalize()).size()
    assert (per_day == n).all()
    if tf != "D":
        assert (r.index.hour * 60 + r.index.minute).min() == 555           # first bar 09:15
    day1 = df[df.index.normalize() == df.index[0].normalize()]
    first = r.iloc[0]
    k = 375 if tf == "D" else store.TIMEFRAMES[tf]
    assert first.open == day1.open.iloc[0]
    assert first.high == day1.high.iloc[:k].max() and first.low == day1.low.iloc[:k].min()
    assert first.close == day1.close.iloc[k - 1]


def test_hourly_bars_match_nse_layout():
    r = store.resample(synthetic_minutes(days=1), "60")
    assert [f"{t:%H:%M}" for t in r.index] == ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]


def test_load_base_and_window_paging():
    df = store.bars("NIFTY 50", "5")
    assert len(df) == 30 * 75
    last = store.window(df, limit=100)
    assert len(last) == 100 and last.index[-1] == df.index[-1]
    older = store.window(df, before=int(store.to_epoch(last.index[:1])[0]), limit=50)
    assert older.index[-1] < last.index[0] and len(older) == 50
    fwd = store.window(df, start=int(store.to_epoch(df.index[10:11])[0]), limit=5)
    assert list(fwd.index) == list(df.index[10:15])


def test_ext_merge_new_data_wins_and_extends():
    sym = "NIFTY BANK"
    base = store.minute_bars(sym)
    last_day = base.index[-1].normalize()
    patch = base[base.index.normalize() == last_day].copy()
    patch["close"] += 1000                                          # corrected bars for an existing day
    nxt = synthetic_minutes(days=1, start=str((last_day + pd.offsets.BDay(1)).date()), seed=99, base=46000)
    assert store.save_ext(sym, pd.concat([patch, nxt])) == len(patch) + len(nxt)
    merged = store.minute_bars(sym)
    assert merged.index[-1] == nxt.index[-1]
    assert np.allclose(merged.loc[patch.index, "close"], patch.close)
    assert not merged.index.duplicated().any() and merged.index.is_monotonic_increasing


def test_five_minute_extension_fills_days_missing_from_1m():
    sym = "TESTSTOCK"
    last = store.minute_bars(sym).index[-1].normalize()
    d5 = store.resample(synthetic_minutes(days=2, start=str((last + pd.offsets.BDay(5)).date()), seed=5, volume=True), "5")
    store.save_ext(sym, d5, "5")
    b15 = store.bars(sym, "15")
    assert b15.index[-1].normalize() == d5.index[-1].normalize()
    assert store.bars(sym, "1").index[-1].normalize() == last          # 1m charts never use 5m data


def test_records_are_json_ready():
    r = store.records(store.bars("NIFTY 50", "15").iloc[:3])
    assert set(r[0]) == {"time", "open", "high", "low", "close", "volume"} and isinstance(r[0]["time"], int)


def test_24h_market_bars_follow_utc_days():
    idx = pd.DatetimeIndex(["2026-01-06 02:10", "2026-01-06 05:29", "2026-01-06 05:30", "2026-01-06 10:40"])
    k60 = store.bucket_key(idx, "60", 330)
    assert [str(x) for x in k60] == ["2026-01-06 01:30:00", "2026-01-06 04:30:00", "2026-01-06 05:30:00", "2026-01-06 10:30:00"]
    kd = store.bucket_key(idx, "D", 330)
    assert [str(x.date()) for x in kd] == ["2026-01-05", "2026-01-05", "2026-01-06", "2026-01-06"]
    # NSE layout is unchanged by the generalisation
    n = pd.DatetimeIndex(["2026-01-06 09:15", "2026-01-06 15:29"])
    assert [str(x) for x in store.bucket_key(n, "60")] == ["2026-01-06 09:15:00", "2026-01-06 15:15:00"]
    assert store._session(pd.DataFrame({"close": [1.0]}, index=pd.DatetimeIndex(["2026-01-06 02:00"])), (330, 1770)).shape[0] == 1
