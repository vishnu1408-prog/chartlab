"""Sideways meter: chance that today ends QUIET (range < 0.8 × average range of the last 10 days), updated every bar
from 09:45. Model = logistic regression trained walk-forward on NIFTY 50 1-minute data 2015–2026
(indicators/tests/sideways_meter.py). Out-of-sample 2022–26: AUC 0.75 at 09:45, 0.81 at 10:15, 0.86 at 11:15; calibrated
(when it says 60–80 %, 80 % of those days were quiet). It predicts how BIG the day is, not its direction, and it is NOT
a profitable breakout filter on its own (tested) — use it for expectations: targets, holding time, whether to bother."""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from ... import store
from ..base import Indicator, Output, Param, line, times

MODEL = json.loads((Path(__file__).parent / "sideways_model.json").read_text())


def _vix_change() -> dict:
    try:
        v = store.bars("INDIA VIX", "D").close
        return (v / v.shift(1) - 1).shift(1).to_dict()            # previous day's change, known before the open
    except Exception:
        return {}


class Sideways(Indicator):
    id, name, overlay = "sideways", "Sideways meter", False
    description = "Chance that today stays quiet (small range) — from 09:45, every bar. Trained on NIFTY 50."
    warmup = 1600                                                   # ≥ 10 sessions of history for the average range
    params = [Param("warn", "float", 70, "Mark 'likely sideways' above %", 50, 95, 5)]

    def compute(self, df, warn):
        if df.empty or len(df) < 30:
            return Output()
        tod = (df.index.hour * 60 + df.index.minute).to_numpy()
        tfm = int(np.median(np.diff(tod[tod > 0]))) if len(df) > 2 else 5
        tfm = tfm if 0 < tfm <= 60 else 5
        day = df.index.normalize()
        D = df.groupby(day).agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"))
        D["rng"] = D.h - D.l
        D["adr"] = D.rng.rolling(10).mean().shift(1)
        D["prev_range"] = (D.rng / D.adr).shift(1)
        D["gap"] = (D.o - D.c.shift(1)).abs() / D.adr
        vix = _vix_change()
        mean, scale = np.array(MODEL["mean"]), np.array(MODEL["scale"])
        coef, b0 = np.array(MODEL["quiet"]["coef"]), MODEL["quiet"]["intercept"]
        h, l, c = df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
        prob = np.full(len(df), np.nan)
        last = None
        starts = np.r_[0, np.flatnonzero(np.diff(day.values.astype("int64"))) + 1, len(df)]
        for a, b in zip(starts[:-1], starts[1:]):
            d = day[a]
            adr = D.at[d, "adr"]
            if not np.isfinite(adr) or adr <= 0 or not np.isfinite(D.at[d, "prev_range"]):
                continue
            o = D.at[d, "o"]
            hmax, lmin = np.maximum.accumulate(h[a:b]), np.minimum.accumulate(l[a:b])
            for k in range(b - a):
                end_min = tod[a + k] + tfm - 555                    # minutes after 09:15 when this bar closes
                if end_min < 30 or end_min > 360:
                    continue
                rs = hmax[k] - lmin[k]
                w0 = k
                while w0 > 0 and tod[a + w0 - 1] >= tod[a + k] + tfm - 30:
                    w0 -= 1
                x = np.array([rs / adr, abs(c[a + k] - o) / adr, (c[a + k] - lmin[k]) / rs if rs > 0 else 0.5,
                              (h[a + w0: a + k + 1].max() - l[a + w0: a + k + 1].min()) / adr, end_min / 375,
                              D.at[d, "prev_range"], D.at[d, "gap"], vix.get(d, 0.0) or 0.0])
                z = b0 + float(((x - mean) / scale) @ coef)
                prob[a + k] = 100 / (1 + math.exp(-z))
                last = (prob[a + k], adr, d, end_min)
        n = len(df)
        cols = np.where(prob >= warn, "#f5a623", "#7e57c2")
        out = Output(series=[line("quiet", "Quiet-day chance %", df, prob, "#f5a623", pane=1, width=2),
                             line("warn", f"{warn:.0f}%", df, np.full(n, warn), "rgba(245,166,35,0.45)", pane=1, width=1),
                             line("half", "50%", df, np.full(n, 50.0), "rgba(120,123,134,0.4)", pane=1, width=1)])
        out.series[0]["data"] = [dict(p, color=cols[i]) if "value" in p else p for i, p in enumerate(out.series[0]["data"])]
        # one marker per day when the chance first crosses the warning level (known at that bar's close)
        t = times(df)
        prev_day, fired = None, False
        for i in range(n):
            if day[i] != prev_day:
                prev_day, fired = day[i], False
            if not fired and np.isfinite(prob[i]) and prob[i] >= warn:
                out.markers.append({"time": int(t[i]), "position": "aboveBar", "shape": "circle", "color": "#f5a623",
                                    "text": "sideways?", "visible_from": int(t[i])})
                fired = True
        if last:
            p, adr, d, end_min = last
            cal = MODEL["calibration"]
            i = min(len(cal["edges"]) - 2, max(0, int(np.searchsorted(cal["edges"], p / 100, side="right") - 1)))
            out.info = {"latest": f"{p:.0f}% quiet at {(555 + end_min) // 60:02d}:{(555 + end_min) % 60:02d}",
                        "typical extra range": f"≈ {cal['extra_range_median_adr'][i] * adr:.0f} pts (80% of days ≤ {cal['extra_range_p80_adr'][i] * adr:.0f})",
                        "ADR": f"{adr:.0f}", "model": "NIFTY 50, AUC 0.81 at 10:15"}
        return out


INDICATORS = [Sideways()]
