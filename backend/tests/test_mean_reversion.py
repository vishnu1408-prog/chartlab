import numpy as np
import pandas as pd

from app.indicators.base import times
from app.indicators.builtin.mean_reversion import MeanReversion, find_signals


def _df(n=600, seed=1):
    rng = np.random.default_rng(seed)
    idx = []
    d = pd.Timestamp("2026-01-05 09:15")
    while len(idx) < n:
        for k in range(75):
            idx.append(d + pd.Timedelta(minutes=5 * k))
        d += pd.Timedelta(days=1)
    idx = pd.DatetimeIndex(idx[:n])
    # mean-reverting series (AR) so stretches + snap-backs occur
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = 0.85 * x[i - 1] + rng.normal(0, 8)
    c = 24000 + x
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0, 5, n)
    l = np.minimum(o, c) - rng.uniform(0, 5, n)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 0.0}, index=idx)


def test_signals_exist_and_sane():
    df = _df()
    sig, m, s, z = find_signals(df)
    assert sig
    for g in sig:
        assert (g["target"] - g["entry"]) * g["side"] >= g["risk"] > 0
        assert (g["entry"] - g["stop"]) * g["side"] > 0


def test_replay_safe():
    df = _df()
    ind = MeanReversion()
    full = ind.compute(df, **ind.resolve({}))
    cut = times(df)[400]
    part = ind.compute(df[times(df) <= cut], **ind.resolve({}))
    a = [(x["time"], x["text"]) for x in full.markers if x["visible_from"] <= cut]
    b = [(x["time"], x["text"]) for x in part.markers]
    assert a == b
    for sid in ("mean", "up1", "dn1"):
        fa = {p["time"]: p.get("value") for p in next(x for x in full.series if x["id"] == sid)["data"] if p["time"] <= cut}
        fb = {p["time"]: p.get("value") for p in next(x for x in part.series if x["id"] == sid)["data"]}
        assert fa.keys() == fb.keys()
        assert all((fa[k] is None and fb[k] is None) or abs(fa[k] - fb[k]) < 1e-6 for k in fa)


def test_all_centers_run():
    df = _df()
    ind = MeanReversion()
    for c in ("EMA", "SMA", "Session mean", "VWAP"):
        out = ind.compute(df, **ind.resolve({"center": c}))
        assert out.series
