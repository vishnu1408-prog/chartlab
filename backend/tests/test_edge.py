"""Morning Edge: trade legs, NSE file parsing, derivatives features, the indicator."""
import io
import zipfile

import numpy as np
import pandas as pd

from app import edge, store
from app.indicators import registry


def _path(d, prices):
    """1-minute bars from 09:45 with the given (high, low) per minute, then flat to 15:15"""
    idx = pd.date_range(d + pd.Timedelta(minutes=585), d + pd.Timedelta(minutes=915), freq="1min")
    hi = np.full(len(idx), 100.5); lo = np.full(len(idx), 99.5)
    for i, (h, l) in enumerate(prices):
        hi[i], lo[i] = h, l
    return pd.DataFrame({"open": np.full(len(idx), 100.0), "high": hi, "low": lo, "close": np.full(len(idx), 100.0),
                         "volume": 0.0}, index=idx)


def test_e2_stop_first_then_half_and_breakeven():
    d = pd.Timestamp("2026-03-02")
    r = edge.e2_legs(_path(d, [(110, 60)]), 1)                         # both in one minute → stop
    assert r["done"] and r["pts"] == -35 and r["legs"][0][3] == "stop"
    r = edge.e2_legs(_path(d, [(101, 99), (136, 101), (120, 99)]), 1)   # +35 then back to entry
    assert [l[3] for l in r["legs"]] == ["+35", "breakeven"] and r["pts"] == 17.5
    m = _path(d, [(101, 99), (136, 101)])
    m.loc[m.index >= d + pd.Timedelta(minutes=587), ["open", "high", "low", "close"]] = 180.0
    r = edge.e2_legs(m, 1)                                              # +35 then runs to 15:15 at 180
    assert [l[3] for l in r["legs"]] == ["+35", "15:15"] and r["pts"] == 0.5 * 35 + 0.5 * 80
    r = edge.e2_legs(_path(d, [(101, 64)]), -1)                          # short: +35 is a fall to 65
    assert r["legs"][0][3] == "+35"


def test_parse_udiff_bhavcopy_keeps_nifty_fut_and_two_expiries():
    rows = [dict(TckrSymb="NIFTY", FinInstrmTp="IDF", OptnTp="", XpryDt="2026-10-27", StrkPric=None, ClsPric=22500,
                 OpnIntrst=100, ChngInOpnIntrst=5, TtlTradgVol=10, UndrlygPric=22450)]
    for exp in ("2026-10-14", "2026-10-21", "2026-10-28"):
        for k in (22400, 22500):
            for typ in ("CE", "PE"):
                rows.append(dict(TckrSymb="NIFTY", FinInstrmTp="IDO", OptnTp=typ, XpryDt=exp, StrkPric=k, ClsPric=50,
                                 OpnIntrst=1000, ChngInOpnIntrst=10, TtlTradgVol=5, UndrlygPric=22450))
    rows.append(dict(TckrSymb="BANKNIFTY", FinInstrmTp="IDO", OptnTp="CE", XpryDt="2026-10-14", StrkPric=1, ClsPric=1,
                     OpnIntrst=1, ChngInOpnIntrst=1, TtlTradgVol=1, UndrlygPric=1))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fo.csv", pd.DataFrame(rows).to_csv(index=False))
    out = edge.parse_bhav(buf.getvalue(), pd.Timestamp("2026-10-13").date())
    assert set(out.kind) == {"FUT", "CE", "PE"} and out.expiry[out.kind != "FUT"].nunique() == 2
    assert len(out) == 1 + 2 * 2 * 2


def test_oi_features_walls_and_pcr():
    d = pd.Timestamp("2026-10-12")
    fo = pd.DataFrame([dict(date=d, kind="FUT", expiry=pd.Timestamp("2026-10-27"), strike=0, close=22500, oi=100, chg_oi=0, und=np.nan),
                       dict(date=d, kind="CE", expiry=pd.Timestamp("2026-10-14"), strike=22600, close=40, oi=900, chg_oi=50, und=np.nan),
                       dict(date=d, kind="CE", expiry=pd.Timestamp("2026-10-14"), strike=22700, close=20, oi=300, chg_oi=0, und=np.nan),
                       dict(date=d, kind="PE", expiry=pd.Timestamp("2026-10-14"), strike=22400, close=35, oi=600, chg_oi=100, und=np.nan)])
    f = edge.oi_features(fo, pd.Series({d: 22500.0}))
    r = f.loc[d]
    assert r.callWall == 22600 and r.putWall == 22400 and abs(r.pcr - 600 / 1200) < 1e-9 and r.dte == 2


def test_indicator_without_model_explains_itself():
    ind = registry.get("morning_edge")
    out = ind.compute(store.bars("NIFTY 50", "5"), **ind.resolve({}))
    assert "model" in out.info.get("note", "") or out.markers == []


def test_indicator_draws_saved_signals(tmp_path):
    df = store.bars("NIFTY 50", "5")
    days = df.index.normalize().unique()
    d = days[-3]
    edge.DIR.mkdir(parents=True, exist_ok=True)
    h = edge.DIR / "history.csv"
    pd.DataFrame({"pred": [0.3, 0.01], "source": ["walk-forward", "walk-forward"]}, index=[d, days[-2]]).to_csv(h)
    try:
        ind = registry.get("morning_edge")
        out = ind.compute(df, **ind.resolve({}))
        arrows = [m for m in out.markers if m["shape"] == "arrowUp"]
        assert len(arrows) == 1 and "ME BUY!" in arrows[0]["text"]                # 0.3 ≥ STRONG
        assert all(m["visible_from"] >= m["time"] for m in out.markers)
        assert "on this chart" in out.info
    finally:
        h.unlink()


def test_vix_prev_comes_from_last_evenings_save_without_a_minute_request(monkeypatch, tmp_path):
    import datetime as dt
    import pandas as pd
    from app import edge, sources
    monkeypatch.setattr(edge, "DIR", tmp_path)
    asked = []

    def candles(sym, a, b, iv="ONE_MINUTE", **k):
        asked.append((iv, a))
        if iv == "ONE_DAY":
            return pd.DataFrame({"close": [15.9, 15.76]}, index=pd.to_datetime(["2026-10-21", "2026-10-22"]))
        if a.hour == 15:                                      # the previous session's last minutes
            return pd.DataFrame({"close": [15.70, 15.725]}, index=pd.to_datetime(["2026-10-22 15:28", "2026-10-22 15:29"]))
        return pd.DataFrame({"open": [15.76]}, index=pd.to_datetime(["2026-10-23 09:15"]))
    monkeypatch.setattr(sources.Angel, "candles", candles)
    assert edge.save_vix_close(dt.datetime(2026, 10, 22, 15, 40)) == {"day": "2026-10-22", "close": 15.725, "at": "15:29"}
    asked.clear()
    v = edge.vix_live(dt.datetime(2026, 10, 23, 9, 45))
    assert v["vix_prev"] == 15.725 and v["vix_open"] == 15.76 and "saved" in v["vix_prev_src"]
    assert not [a for iv, a in asked if iv == "ONE_MINUTE" and a.hour == 15]     # no 09:45 request for yesterday
