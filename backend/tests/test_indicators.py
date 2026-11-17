import json
import shutil
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app import config, store
from app.indicators import registry
from app.indicators.base import Param
from conftest import REAL_INDEX

EXAMPLE = Path(__file__).resolve().parents[2] / "custom_indicators" / "example_inside_bar.py"


def ids():
    return sorted(registry.all_indicators())


def test_builtins_present_and_metadata_serialisable():
    want = {"sma", "ema", "bb", "rsi", "macd", "supertrend", "vwap", "ema_cross", "pivots", "orb", "vprofile", "harmonic", "sideways"}
    assert want <= set(ids())
    for ind in registry.all_indicators().values():
        json.dumps(ind.meta())
        assert ind.resolve({}) == {p.name: p.default for p in ind.params}


def test_param_coercion_and_clamping():
    p = Param("n", "int", 10, min=1, max=50)
    assert p.coerce("7") == 7 and p.coerce(500) == 50 and p.coerce("x") == 10 and p.coerce(None) == 10
    assert Param("b", "bool", False).coerce("true") is True
    assert Param("c", "choice", "a", options=["a", "b"]).coerce("z") == "a"


def series(out, sid):
    s = next(x for x in out.series if x["id"] == sid)
    return np.array([d.get("value", np.nan) for d in s["data"]])


def test_ema_sma_rsi_macd_values():
    df = store.bars("NIFTY 50", "15").iloc[:600]
    reg = registry.all_indicators()
    np.testing.assert_allclose(series(reg["ema"].compute(df, length=21, source="close", color="#fff"), "ema"),
                               df.close.ewm(span=21, adjust=False).mean().to_numpy())
    sma = series(reg["sma"].compute(df, length=20, source="close", color="#fff"), "sma")
    np.testing.assert_allclose(sma[19:], df.close.rolling(20).mean().to_numpy()[19:])
    assert np.isnan(sma[:19]).all()
    rsi = series(reg["rsi"].compute(df, length=14, upper=70, lower=30), "rsi")
    assert np.nanmin(rsi) >= 0 and np.nanmax(rsi) <= 100 and np.isnan(rsi[:14]).all()
    m = reg["macd"].compute(df, fast=12, slow=26, signal=9)
    macd = series(m, "macd")
    exp = (df.close.ewm(span=12, adjust=False).mean() - df.close.ewm(span=26, adjust=False).mean()).to_numpy()
    np.testing.assert_allclose(macd, exp)


def test_supertrend_lines_are_on_the_correct_side():
    df = store.bars("NIFTY 50", "5").iloc[:1500]
    out = registry.get("supertrend").compute(df, length=10, mult=3.0)
    up, dn = series(out, "up"), series(out, "down")
    c = df.close.to_numpy()
    assert (up[np.isfinite(up)] <= c[np.isfinite(up)]).all()
    assert (dn[np.isfinite(dn)] >= c[np.isfinite(dn)]).all()


def test_vwap_needs_volume():
    assert "note" in registry.get("vwap").compute(store.bars("NIFTY 50", "5").iloc[:200], color="#fff").info
    out = registry.get("vwap").compute(store.bars("TESTSTOCK", "5").iloc[:200], color="#fff")
    v = series(out, "vwap")
    assert np.isfinite(v).all()


def _clip(shape, cut):
    pts = shape["points"]
    if shape.get("clip"):
        pts = [{**p, "time": min(p["time"], cut)} for p in pts]
    return json.dumps({**shape, "points": pts}, sort_keys=True)


def check_no_lookahead(ind, df, cut_frac=0.6, params=None):
    """The replay contract: computing on data that ends at the cut must show exactly what the full computation
    shows up to the cut (values ≤ cut equal; markers/shapes with visible_from ≤ cut identical, after clipping)."""
    p = ind.resolve(params or {})
    k = int(len(df) * cut_frac)
    cut = int(store.to_epoch(df.index[k - 1:k])[0])
    full, part = ind.compute(df, **p), ind.compute(df.iloc[:k], **p)
    for sf, sp in zip(full.series, part.series):
        vf = {d["time"]: d.get("value") for d in sf["data"] if d["time"] <= cut}
        vp = {d["time"]: d.get("value") for d in sp["data"]}
        assert vf.keys() == vp.keys(), ind.id
        for t in vf:
            a, b = vf[t], vp[t]
            assert (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= 1e-9 * max(1, abs(a))), (ind.id, sf["id"], t)
    mk = lambda o: sorted(json.dumps(m, sort_keys=True) for m in o.markers if m["visible_from"] <= cut)
    assert mk(full) == mk(part), ind.id
    sh = lambda o: sorted(_clip(s, cut) for s in o.shapes if s["visible_from"] <= cut and not s.get("live_only"))
    assert sh(full) == sh(part), ind.id
    for m in full.markers:
        assert m["visible_from"] >= m["time"] or ind.id == "harmonic", ind.id     # nothing is known before its bar


@pytest.mark.parametrize("ind_id", ["sma", "ema", "bb", "rsi", "macd", "supertrend", "vwap", "ema_cross", "sideways",
                                    "pivots", "orb", "vprofile", "harmonic", "fakeout", "gann_drive", "morning_edge", "daily_plan",
                                    "wyckoff"])
@pytest.mark.parametrize("tf", ["5", "15"])
def test_every_builtin_respects_the_replay_contract(ind_id, tf):
    sym = "TESTSTOCK" if ind_id == "vwap" else "NIFTY 50"
    df = store.bars(sym, tf)
    for frac in (0.37, 0.61, 0.83):
        check_no_lookahead(registry.get(ind_id), df, frac)


def test_harmonic_all_patterns_finds_trades_and_respects_replay():
    ind = registry.get("harmonic")
    df = store.bars("NIFTY 50", "5")
    allp = {k: True for k in ("gartley", "bat", "altbat", "butterfly", "crab", "deepcrab", "shark", "cypher", "abcd")}
    out = ind.compute(df, **ind.resolve({**allp, "maxPerDay": 10}))
    assert out.info.get("trades"), "synthetic data should produce at least one harmonic trade"
    check_no_lookahead(ind, df, 0.5, {**allp, "maxPerDay": 10})


@pytest.mark.skipif(not REAL_INDEX.exists(), reason="Kaggle NIFTY data not present")
def test_harmonic_on_real_nifty_matches_research_engine_and_replay(monkeypatch):
    import importlib
    from app import symbols
    real = sorted(REAL_INDEX.glob("versions/*/"), key=lambda p: int(p.name))[-1]
    monkeypatch.setattr(config, "INDEX_DIR", real)
    symbols.all_symbols.cache_clear()
    try:
        raw = pd.read_csv(real / "NIFTY 50_minute.csv", parse_dates=["date"]).set_index("date").iloc[-60000:]
        df = store.resample(store._clean(raw), "15")
        ind = registry.get("harmonic")
        out = ind.compute(df, **ind.resolve({}))
        assert int(out.info.get("trades", "0")) > 0
        check_no_lookahead(ind, df, 0.7)
    finally:
        symbols.all_symbols.cache_clear()


def test_custom_indicator_hot_reload_and_errors(tmpdirs):
    cdir = Path(config.CUSTOM_DIR)
    shutil.copy(EXAMPLE, cdir / "inside.py")
    assert "inside_bar" in ids()
    check_no_lookahead(registry.get("inside_bar"), store.bars("NIFTY 50", "5"))
    (cdir / "broken.py").write_text("import nonexistent_module_xyz\n")
    errs = registry.errors()
    assert any(e["file"] == "broken.py" and "nonexistent_module_xyz" in e["error"] for e in errs)
    assert "inside_bar" in ids()                                              # one bad file does not hide the others
    src = (cdir / "inside.py").read_text().replace('name = "Inside Bar Breakout (example)"', 'name = "Renamed"')
    time.sleep(0.01)
    (cdir / "inside.py").write_text(src)
    import os
    os.utime(cdir / "inside.py", (time.time() + 5, time.time() + 5))
    assert registry.get("inside_bar").name == "Renamed"
    (cdir / "broken.py").unlink()
    (cdir / "inside.py").unlink()
    assert "inside_bar" not in ids() and not registry.errors()


def test_fakeout_checker_marks_breakouts_and_results():
    from app.indicators.builtin.fakeout import fake_prob
    ind = registry.get("fakeout")
    out = ind.compute(store.bars("NIFTY 50", "5"), **ind.resolve({}))
    calls = [m for m in out.markers if "fake " in m["text"]]
    results = [m for m in out.markers if m["text"] in ("✗ fake", "✓ held 30m")]
    assert calls and results and len(results) <= len(calls)
    assert all(0 < int(m["text"].split()[-1].rstrip("%")) < 100 for m in calls)
    base = dict(level="PDH", minute=600, body_atr=1, range_atr=1.2, wick_against=0.1, touches=1, move_adr=0.2,
                gap_with=1.0, with_trend=1.0, prev_rng_adr=1.0)
    assert fake_prob({**base, "beyond_atr": 0.05}) > fake_prob({**base, "beyond_atr": 1.0})    # just past the level → more fakes
    assert "5-minute" in ind.compute(store.bars("NIFTY 50", "15"), **ind.resolve({})).info["note"]



def _gann_days(drive_body, run_to):
    import numpy as np
    import pandas as pd
    rows = []
    for k, d in enumerate(pd.bdate_range("2026-03-02", periods=12)):
        idx = pd.date_range(d + pd.Timedelta(minutes=555), d + pd.Timedelta(minutes=925), freq="5min")
        o = np.full(len(idx), 22500.0)
        c = o.copy()
        if k == 11:                                           # last day: 09:15 body, 09:20 closes above G+1 (≈22575)
            c[0] = 22500 + drive_body
            o[1:], c[1:] = 22580.0, 22580.0
            c[4] = run_to
        rows.append(pd.DataFrame({"open": o, "high": np.maximum(o, c) + 20, "low": np.minimum(o, c) - 20, "close": c,
                                  "volume": 0.0}, index=idx))
    return pd.concat(rows)                                    # earlier days: range 40 → ADR10 = 40


def test_gann_drive_levels_signal_and_exits():
    import numpy as np
    from app.indicators.builtin.gann_drive import day_trades, tally
    r = day_trades(_gann_days(20, 22610), None)[-1]           # body 20 = 0.5 × ADR → strong; bar 4 high 22630 ≥ target
    assert abs(r["levels"][1] - (np.sqrt(22500) + 0.25) ** 2) < 1e-6
    assert r["strong"] and r["dir"] == 1 and r["entry"] == 22580
    assert r["why"] == "target" and abs(r["pts"] - (35 - 0.0002 * 22580)) < 1e-9
    assert "1 trades · win 100%" in tally([r])
    w = day_trades(_gann_days(5, 22610), None)[-1]            # body 5 = 0.125 × ADR → weak drive: signal shown, not counted
    assert not w["strong"] and w["dir"] == 1 and tally([w]) == "no trades yet"
    s = day_trades(_gann_days(20, 22530), None)[-1]           # bar 4 low 22510 ≤ stop 22545
    assert s["why"] == "stop" and s["pts"] < -35


def test_gann_drive_indicator_on_real_data():
    ind = registry.get("gann_drive")
    out = ind.compute(store.bars("NIFTY 50", "5"), **ind.resolve({}))
    assert any(sh["text"] == "G+1" for sh in out.shapes)
    assert any(m["text"].startswith("G ") for m in out.markers)               # signals (traded or "skip")
    assert "live since 13 Oct 2026" in out.info
    assert "5-minute" in ind.compute(store.bars("NIFTY 50", "15"), **ind.resolve({})).info["note"]


def test_gann_drive_ema_filter_trades_a_subset_and_keeps_the_replay_contract():
    ind = registry.get("gann_drive")
    df = store.bars("NIFTY 50", "5")
    plain = ind.compute(df, **ind.resolve({}))
    filt = ind.compute(df, **ind.resolve({"ema_filter": True}))
    n = lambda o: sum(m["text"].startswith("G ") and "skip" not in m["text"] for m in o.markers)
    assert n(filt) <= n(plain)                                               # the filter only removes trades
    assert "EMA 9/21" in filt.info["backtest"]
    check_no_lookahead(ind, df, params={"ema_filter": True})


def test_ng_fvg_trades_a_bullish_gap_retest_and_holds_to_2300():
    import numpy as np
    import pandas as pd
    from app.indicators.builtin.ng_fvg import fvg_trades
    idx = pd.date_range("2026-10-19 09:00", "2026-10-19 23:25", freq="5min")
    n = len(idx)
    o = np.full(n, 300.0); h = o + 0.5; l = o - 0.5; c = o.copy()
    k = 12                                                   # 10:00: displacement candle k-1, gap above bar k-2
    o[k - 1], c[k - 1], h[k - 1], l[k - 1] = 300.0, 306.0, 306.2, 299.9
    o[k:], c[k:], h[k:], l[k:] = 306.5, 306.5, 307.0, 302.0  # bar k low 302 > high[k-2] 300.5 → gap 300.5–302
    l[k + 1:] = 303.0
    l[k + 3] = 301.8                                         # pull-back into the gap → limit fill at 302
    h[k + 10:], c[k + 10:], o[k + 10:] = 315.0, 314.0, 314.0
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)
    gaps, tr = fvg_trades(df, cost_pct=0.0)
    assert any(g["side"] == 1 and g["bot"] == 300.5 and g["top"] == 302.0 for g in gaps)
    t = tr[0]
    assert t["side"] == 1 and t["entry"] == 302.0 and t["stop"] < 300.5 and t["why"] == "23:00"
    assert df.index[t["exit_i"]] == pd.Timestamp("2026-10-19 23:00") and t["pts"] == 314.0 - 302.0


def test_mid_level_call_on_wick_touch_from_above():
    import numpy as np
    import pandas as pd
    from app.indicators.builtin.mid_level import mid_trades
    rows = []
    for d, (lo, hi) in (("2026-10-22", (22217.0, 22612.0)), ("2026-10-23", (None, None))):
        for k in range(75):
            t = pd.Timestamp(d) + pd.Timedelta(minutes=555 + 5 * k)
            if lo is not None:                               # prev day: range 22217–22612 → level 22414.5
                p = lo if k == 10 else hi if k == 50 else 22400.0
                rows.append((t, p, max(p, 22400.0), min(p, 22400.0), p))
            elif k == 2:                                     # 09:25 candle: opens above, wick to 22410, closes above
                rows.append((t, 22430.0, 22435.0, 22410.0, 22428.0))
            elif k > 2:
                rows.append((t, 22430.0 + 4 * (k - 3), 22432.0 + 4 * (k - 3), 22428.0 + 4 * (k - 3), 22431.0 + 4 * (k - 3)))
            else:
                rows.append((t, 22440.0, 22445.0, 22435.0, 22440.0))
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close"]).set_index("t")
    lv, tr = mid_trades(df, None, cost=0.0)
    assert round(lv[pd.Timestamp("2026-10-23")][2], 1) == 22414.5
    t = tr[0]
    assert t["side"] == 1 and t["entry_t"] == pd.Timestamp("2026-10-23 09:30") and t["entry"] == 22430.0
    assert t["why"] == "target" and t["gross"] == 30.0


def test_fib_day_levels_from_the_previous_day():
    import pandas as pd
    from app.indicators.builtin.fib_day import anchors, fib_levels
    idx = pd.date_range("2026-10-26 09:15", periods=75, freq="5min").append(pd.date_range("2026-10-27 09:15", periods=75, freq="5min"))
    import numpy as np
    p = np.r_[np.linspace(22200, 22600, 75), np.full(75, 22500.0)]
    df = pd.DataFrame({"open": p, "high": p + 1, "low": p - 1, "close": p}, index=idx)
    (d, hi, lo, up, a, b), = anchors(df, "Previous day", 555)
    assert d == pd.Timestamp("2026-10-27") and hi == 22601 and lo == 22199 and up and a == 75 and b == 149
    lv = dict(fib_levels(hi, lo, up, False))
    assert lv[0.0] == 22601 and lv[1.0] == 22199 and round(lv[0.618], 2) == round(22601 - 0.618 * 402, 2)
    (_, h1, l1, _, a1, _), *_ = anchors(df, "First hour", 555)
    assert a1 == 12 and h1 == float(df.high.iloc[:12].max())          # drawn only after the first hour


def test_wyckoff_pure_helpers():
    from app.indicators.builtin import wyckoff as W
    h = np.array([1, 2, 3, 5, 3, 2, 1, 4, 4, 2, 1.0])
    ph = W.pivots(h, 2, True)
    assert ph[5] == 5 and np.isnan(ph[:5]).all()                     # pivot at bar 3 is known 2 bars later (bar 5)
    assert ph[9] == 4 and np.isnan(ph[10])                           # equal highs: the first one (bar 7) is the pivot
    assert W.pivots(-h, 2, False)[5] == -5
    hh, ll, cc = np.arange(20.0) + 2, np.arange(20.0), np.arange(20.0) + 1
    atr = W.pine_atr(hh, ll, cc, 14)
    assert np.isnan(atr[:13]).all() and atr[13] == 2.0 and abs(atr[19] - 2.0) < 1e-12
    assert W.robust_edge(10.0, 11.0, 12.0, 10.5, True) == 11.0 and W.robust_edge(NA := float("nan"), NA, NA, 10.5, True) == 10.5
    assert W.phase_b_min_bars(4, 2.0) == 30 and W.phase_b_min_bars(10, 9.0) == 60
    assert W.validation_score(40, True, 3.0, 2, 1, 2, True, 3.0, True, True, True, False) == 100
    assert W.structure_confidence(True, True, True, True, False, False, False, False) == 50


def test_wyckoff_runs_and_reveals_events_only_when_known():
    from app.indicators.builtin import wyckoff as W
    df = store.bars("NIFTY 50", "5")
    t0 = time.time()
    r = W.run_engine(df)
    assert time.time() - t0 < 10
    assert r["campaigns"], "synthetic data should seed at least one climax"
    for c in r["campaigns"]:
        assert c["events"][0]["kind"] == "CL" and c["stop"] in (1, -1)
        for e in c["events"]:
            assert e["known"] >= e["bar"]
    for e in r["entries"]:
        assert e["known"] >= e["bar"] and e["dir"] in (1, -1)
    ind = registry.get("wyckoff")
    for trig in ind.params[1].options:
        out = ind.compute(df, **ind.resolve({"min_phase": "A", "trigger": trig, "strictness": "Aggressive"}))
        assert out.info["structure"] and all(m["visible_from"] >= m["time"] for m in out.markers)
        assert all(s["visible_from"] >= min(p["time"] for p in s["points"]) for s in out.shapes)
    check_no_lookahead(ind, df, 0.55, {"min_phase": "A", "strictness": "Aggressive", "trigger": "SOS / SOW"})


def test_wyckoff_tally_trades_next_open_with_stop_first_and_square_off():
    from app.indicators.builtin.wyckoff import trade_entries
    idx = pd.date_range("2026-10-26 09:15", periods=75, freq="5min")
    p = np.full(75, 22000.0)
    df = pd.DataFrame({"open": p, "high": p + 2, "low": p - 2, "close": p, "volume": 0.0}, index=idx)
    df.iloc[5, df.columns.get_loc("open")] = 22001.0                 # entry bar (signal on bar 4)
    df.iloc[8, df.columns.get_loc("high")] = 22040.0                 # target 22031 hit
    tr, = trade_entries(df, [{"known": 4, "dir": 1}], None, 30, 30, cost=0.0)
    assert tr["entry_t"] == idx[5] and tr["entry"] == 22001.0 and tr["why"] == "target" and tr["gross"] == 30.0
    df.iloc[8, df.columns.get_loc("low")] = 21960.0                  # both in one bar → stop first
    tr, = trade_entries(df, [{"known": 4, "dir": 1}], None, 30, 30, cost=0.0)
    assert tr["why"] == "stop" and tr["gross"] == -30.0
    tr, = trade_entries(df, [{"known": 60, "dir": -1}], None, 300, 300, cost=0.0)
    assert tr["why"] == "15:15" and tr["exit_t"] == pd.Timestamp("2026-10-26 15:15")
    assert trade_entries(df, [{"known": 74, "dir": 1}], None) == []   # no next bar → no trade
