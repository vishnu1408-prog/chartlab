import numpy as np
import pandas as pd

from app.indicators.base import times
from app.indicators.builtin.standard import Bollinger


def _df(n=400):
    rng = np.random.default_rng(3)
    idx = pd.date_range("2026-01-05 09:15", periods=n, freq="5min")
    c = 24000 + np.cumsum(rng.normal(0, 5, n))
    c[200:260] = c[199] + rng.normal(0, 0.5, 60)          # quiet stretch → squeeze
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 2, "low": np.minimum(o, c) - 2, "close": c,
                         "volume": 0.0}, index=idx)


def test_matches_formula_and_old_params():
    df = _df()
    out = Bollinger().compute(df, **Bollinger().resolve({"length": 20, "mult": 2, "color": "#123456"}))
    up = {p["time"]: p.get("value") for p in next(s for s in out.series if s["id"] == "upper")["data"]}
    ref = df.close.rolling(20).mean() + 2 * df.close.rolling(20).std(ddof=0)
    t = times(df)
    assert abs(up[int(t[-1])] - ref.iloc[-1]) < 1e-6


def test_squeeze_and_replay():
    df = _df()
    b = Bollinger()
    p = b.resolve({"outside": True, "extra": "1σ + 3σ"})
    full = b.compute(df, **p)
    assert any(m["text"] == "SQZ" for m in full.markers)
    cut = times(df)[300]
    part = b.compute(df[times(df) <= cut], **p)
    assert [(m["time"], m["text"]) for m in full.markers if m["visible_from"] <= cut] == \
           [(m["time"], m["text"]) for m in part.markers]
