import numpy as np
import pandas as pd

from app.indicators.base import times
from app.indicators.builtin.candle_patterns import CandlePatterns, PATTERNS, detect


def _df(rows, start="2024-01-02 09:15", freq="15min"):
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=0)


def _trend_down(n=12, p=100.0):
    rows = []
    for _ in range(n):
        rows.append([p, p + 1, p - 3, p - 2]); p -= 2
    return rows, p


def test_bullish_harami_and_doji():
    rows, p = _trend_down()
    rows.append([p, p + 0.5, p - 8.5, p - 8])        # long red candle
    rows.append([p - 6, p - 3, p - 7.5, p - 4])      # small green body inside it -> bullish harami
    rows.append([p - 4, p - 2, p - 6, p - 3.9])      # doji (body 0.1 of range 4)
    R = detect(_df(rows))
    assert R["bull_harami"][13] and not R["bear_harami"][13]
    assert R["doji"][14]


def test_bearish_harami():
    rows, p = [], 100.0
    for _ in range(12):
        rows.append([p, p + 3, p - 1, p + 2]); p += 2
    rows.append([p, p + 8.5, p - 0.5, p + 8])        # long green
    rows.append([p + 6, p + 7.5, p + 3, p + 4])      # small red inside -> bearish harami
    R = detect(_df(rows))
    assert R["bear_harami"][13] and not R["bull_harami"][13]


def test_switches_and_confirmation_are_replay_safe():
    rng = np.random.default_rng(1)
    c = 20000 + np.cumsum(rng.normal(0, 15, 400))
    o = np.r_[c[0], c[:-1]] + rng.normal(0, 2, 400)
    h = np.maximum(o, c) + rng.uniform(0, 10, 400)
    l = np.minimum(o, c) - rng.uniform(0, 10, 400)
    df = _df(np.c_[o, h, l, c], freq="5min")
    ind = CandlePatterns()
    off = ind.resolve({k: False for k, *_ in PATTERNS})
    assert ind.compute(df, **off).markers == []
    only = ind.resolve({**{k: False for k, *_ in PATTERNS}, "doji": True})
    assert all("Doji" in m["text"] for m in ind.compute(df, **only).markers)
    p = ind.resolve({"confirm": "Next candle"})
    full = ind.compute(df, **p).markers
    cut = int(times(df)[250])
    part = ind.compute(df.iloc[:251], **p).markers
    key = lambda ms: sorted((m["time"], m["text"]) for m in ms if m["visible_from"] <= cut)
    assert key(full) == key(part)
    assert all(m["visible_from"] >= m["time"] for m in full)
