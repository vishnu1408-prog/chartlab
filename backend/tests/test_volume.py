import numpy as np
import pandas as pd

from app.indicators.builtin.volume import Volume


def test_volume_pane():
    rng = np.random.default_rng(2)
    idx = pd.date_range("2026-01-05 09:15", periods=1500, freq="5min")
    c = 24000 + np.cumsum(rng.normal(0, 5, len(idx)))
    df = pd.DataFrame({"open": np.r_[c[0], c[:-1]], "high": c + 3, "low": c - 3, "close": c,
                       "volume": rng.uniform(100, 500, len(idx))}, index=idx)
    out = Volume().compute(df, **Volume().resolve({"days": 3}))
    vol = next(s for s in out.series if s["id"] == "vol")
    assert vol["pane"] == 1 and len(vol["data"]) == len(df)
    assert abs(vol["data"][-1]["value"] - df.volume.iloc[-1]) < 1e-9
    assert "Last closed candle" in out.info
    assert Volume().compute(df.assign(volume=0.0), **Volume().resolve({})).info["note"]
