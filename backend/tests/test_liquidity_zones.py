import numpy as np
import pandas as pd

from app.indicators.base import times
from app.indicators.builtin.liquidity_zones import LiquidityZones, find_zones


def _df(n=500, vol=True, seed=5):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-05 09:15", periods=n, freq="5min")
    c = 24000 + np.cumsum(rng.normal(0, 6, n)) + 40 * np.sin(np.arange(n) / 15)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0, 6, n)
    l = np.minimum(o, c) - rng.uniform(0, 6, n)
    v = rng.uniform(1e5, 5e5, n) if vol else np.zeros(n)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)


def test_zones_and_labels():
    df = _df()
    zones, has_vol = find_zones(df, 10)
    assert has_vol and zones
    for z in zones:
        assert z["top"] > z["bot"] and z["value"] > 0 and z["i_conf"] == z["i_piv"] + 10
    for z in find_zones(df, 5, right=1)[0]:
        assert z["i_conf"] == z["i_piv"] + 1
    out = LiquidityZones().compute(df, **LiquidityZones().resolve({}))
    assert any(s["kind"] == "label" and s["text"].endswith("M") or s["text"].endswith("K") for s in out.shapes
               if s["kind"] == "label")


def test_no_volume_uses_touch_count():
    out = LiquidityZones().compute(_df(vol=False), **LiquidityZones().resolve({}))
    labels = [s["text"] for s in out.shapes if s["kind"] == "label"]
    assert labels and all(x.rstrip("?").endswith("×") for x in labels)


def test_replay_safe():
    df = _df()
    ind = LiquidityZones()
    p = ind.resolve({"breaks": True, "min_pct": 30, "right": 1})
    full = ind.compute(df, **p)
    cut = times(df)[300]
    part = ind.compute(df[times(df) <= cut], **p)
    key = lambda s: (s["kind"], s["points"][0]["time"], round(s["points"][0]["price"], 6), s.get("text"))
    a = sorted(key(s) for s in full.shapes if s["visible_from"] <= cut)
    b = sorted(key(s) for s in part.shapes)
    assert a == b
    assert [(m["time"], m["text"]) for m in full.markers if m["visible_from"] <= cut] == \
           [(m["time"], m["text"]) for m in part.markers]


def test_forming_candle_is_ignored(monkeypatch):
    import app.indicators.builtin.liquidity_zones as lz
    df = _df()
    ind = LiquidityZones()
    p = ind.resolve({"right": 1})
    monkeypatch.setattr(lz, "_forming", lambda d, now=None: True)
    live = ind.compute(df, **p)
    monkeypatch.setattr(lz, "_forming", lambda d, now=None: False)
    closed = ind.compute(df.iloc[:-1], **p)
    lab = lambda o: sorted((s["points"][0]["time"], s["text"]) for s in o.shapes if s["kind"] == "label")
    assert lab(live) == lab(closed)
    last_t = int(times(df)[-1])
    assert all(s["visible_from"] < last_t for s in live.shapes)
    assert all(m["time"] < last_t for m in live.markers)


def test_forming_detection():
    import app.indicators.builtin.liquidity_zones as lz
    df = _df(50)
    end = df.index[-1]
    assert lz._forming(df, now=end + pd.Timedelta(minutes=2))
    assert not lz._forming(df, now=end + pd.Timedelta(minutes=5))


def _shown(shapes, cut):
    return sorted((s["kind"], s["points"][0]["time"], round(s["points"][0]["price"], 6), s.get("text"))
                  for s in shapes if s["visible_from"] <= cut and (s.get("visible_until") is None or s["visible_until"] > cut))


def test_early_zones_replay_safe():
    import app.indicators.builtin.liquidity_zones as lz
    lz_forming = lz._forming
    lz._forming = lambda d, now=None: False
    try:
        df = _df()
        ind = LiquidityZones()
        p = ind.resolve({"right": 1, "early": "All candidates"})
        full = ind.compute(df, **p)
        early = [s for s in full.shapes if s["kind"] == "label" and s["text"].endswith("?")]
        assert early
        t = times(df)
        pos = {int(x): i for i, x in enumerate(t)}
        for s in early:                             # appears at the swing candle, withdrawn 1 candle later at most
            assert s["visible_from"] == s["points"][0]["time"]
            if "visible_until" in s:
                assert pos[s["visible_until"]] - pos[s["visible_from"]] == 1
        for k in (250, 251, 300, 377):
            cut = int(t[k])
            part = ind.compute(df[t <= cut], **p)
            assert _shown(full.shapes, cut) == _shown(part.shapes, cut)
    finally:
        lz._forming = lz_forming


def test_hv_marks():
    import app.indicators.builtin.liquidity_zones as lz
    df = _df(1400)                                                     # 5-min, 24 h per day → 288 slots/day
    df.iloc[1200, df.columns.get_loc("volume")] = 5e6                # a huge-volume candle …
    df.iloc[1200, df.columns.get_loc("high")] = df.high.iloc[1180:1200].max() + 50   # … making a new high
    marks = lz._hv_marks(df, times(df), 5, 1.8, 3, "Both", "#f00", "#0f0")
    t = times(df)
    assert any(m["time"] == int(t[1200]) and m["text"].startswith("HV") for m in marks)
    assert all(m["visible_from"] == m["time"] for m in marks)
    rel, usual = lz.rel_volume(df, 3)
    i = 1300                                                          # 'usual' uses earlier days only
    slot = df.index[i].hour * 60 + df.index[i].minute
    prev = df.volume[(df.index < df.index[i]) & (df.index.hour * 60 + df.index.minute == slot)].iloc[-3:].mean()
    assert abs(usual[i] - prev) < 1e-6
    out = LiquidityZones().compute(_df(vol=False), **LiquidityZones().resolve({}))
    assert not any(m["text"].startswith("HV") for m in out.markers)  # no volume → no HV marks


def test_early_hv_only_is_subset():
    import app.indicators.builtin.liquidity_zones as lz
    f0 = lz._forming
    lz._forming = lambda d, now=None: False
    try:
        df = _df(1400)
        ind = LiquidityZones()
        lab = lambda o: {s["points"][0]["time"] for s in o.shapes if s["kind"] == "label" and s["text"].endswith("?")}
        every = lab(ind.compute(df, **ind.resolve({"right": 1, "early": "All candidates"})))
        hv = lab(ind.compute(df, **ind.resolve({"right": 1, "early": "HV candles only", "hv_mult": 1.3, "hv_days": 3})))
        assert hv and hv < every
        assert not lab(ind.compute(_df(vol=False), **ind.resolve({"right": 1})))   # no volume → no HV early zones
    finally:
        lz._forming = f0
