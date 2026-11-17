import numpy as np
import pandas as pd

from app.indicators.builtin import orderflow as of


def _day(volume=True):
    idx = pd.date_range("2026-10-16 09:15", periods=60, freq="5min")
    px = np.r_[np.full(40, 100.0), np.linspace(100, 110, 20)]          # most time / volume around 100
    v = np.r_[np.full(40, 1000.0), np.full(20, 100.0)] if volume else np.zeros(60)
    return pd.DataFrame({"open": px, "high": px + 0.5, "low": px - 0.5, "close": px, "volume": v}, index=idx)


def test_value_area_finds_the_heavy_price_and_holds_70_percent():
    edges, vol = of.bin_profile(np.array([101.0, 105, 110]), np.array([99.0, 104, 109]), np.array([10.0, 1, 1]), 22)
    poc, vah, val = of.value_area(edges, vol)
    assert 99 <= poc <= 101 and val <= poc <= vah
    inside = vol[(edges[:-1] >= val - 1e-9) & (edges[1:] <= vah + 1e-9)].sum()
    assert inside >= 0.7 * vol.sum() - 1e-9


def test_profile_and_vwap_use_volume_or_fall_back_to_time():
    out = of.SessionProfile().compute(_day(), days=1, bins=20, width=0.35, extend=True, color="#5c6bc0")
    assert out.info["mode"] == "volume at price" and any(s.get("text") == "POC" for s in out.shapes)
    poc = next(s for s in out.shapes if s.get("text") == "POC")
    assert abs(poc["p1"] - 100) < 1.5 if "p1" in poc else True
    out2 = of.SessionProfile().compute(_day(volume=False), days=1, bins=20, width=0.35, extend=True, color="#5c6bc0")
    assert out2.info["mode"].startswith("time at price")
    v = of.VWAPBands().compute(_day(), anchor="session", band1=1.0, band2=2.0, color="#e040fb")
    assert [s["title"] for s in v.series][0].startswith("VWAP") and len(v.series) == 5
    last = v.series[0]["data"][-1]["value"]
    assert 100 < last < 102                                 # dominated by the heavy volume at 100
    t = of.VWAPBands().compute(_day(volume=False), anchor="session", band1=1.0, band2=0.0, color="#e040fb")
    assert t.series[0]["title"].startswith("TWAP") and len(t.series) == 3


def test_footprint_aggregates_trade_sides_into_minutes_and_candles():
    from app import footprint as fp
    t0 = pd.Timestamp("2026-10-18 10:00:00").value // 10**6 - 5.5 * 3600 * 1000      # 10:00 IST in UTC ms
    ts = np.array([t0, t0 + 10_000, t0 + 70_000, t0 + 130_000], dtype=np.int64)
    price = np.array([100.4, 100.6, 101.2, 101.9])
    qty = np.array([1.0, 2.0, 3.0, 4.0])
    sell = np.array([True, False, False, True])
    rows = fp.aggregate(ts, price, qty, sell, 1.0)
    assert set(rows.minute.dt.strftime("%H:%M")) == {"10:00", "10:01", "10:02"}
    m0 = rows[rows.minute == pd.Timestamp("2026-10-18 10:00")]
    assert m0.buy.sum() == 2.0 and m0.sell.sum() == 1.0
    c = fp.candles(rows, pd.DatetimeIndex([pd.Timestamp("2026-10-18 10:00")]), 5, 1.0)
    k = pd.Timestamp("2026-10-18 10:00")
    assert c[k]["delta"] == (2 + 3) - (1 + 4) and c[k]["poc"] == 101.0          # 101: 3 + 4 = 7 traded
    assert [r[0] for r in c[k]["rows"]] == [101.0, 100.0]                      # high price first
