"""Sideways meter v2: chance that today ends QUIET (range < 0.8 × average range of the last 10 days), updated every bar
from 09:45, plus the day's likely high / low limits on the price chart. Model = logistic regression trained walk-forward
on NIFTY 50 1-minute data 2015 → May 2026 (indicators/tests/sideways_meter2.py, sideways_export_v2.py).
v2 adds distance from TWAP, efficiency so far, the VIX-implied move vs ADR, days to expiry and ADR5/ADR20 to v1's 8
inputs. Out-of-sample 2022–26: AUC 0.76 at 09:45, 0.82 at 10:15, 0.86 at 11:15; calibrated (70–80 % → 80 % quiet).
It predicts how BIG the day is, not its direction, and it is NOT a profitable breakout filter on its own (tested) —
use it for expectations: targets, holding time, whether to bother."""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from ... import store
from ..base import Indicator, Output, Param, line, times

MODEL = json.loads((Path(__file__).parent / "sideways_model.json").read_text())


def _vix_closes() -> pd.Series:
    """India VIX daily closes by date: stored daily bars, topped up with the closes Morning Edge saved each morning
    (vix_prev of day d = the close of the trading day before d) and the evening-saved close."""
    try:
        v = store.bars("INDIA VIX", "D").close.copy()
        v.index = v.index.normalize()
    except Exception:
        v = pd.Series(dtype="float64")
    out = v.to_dict()
    try:
        from ... import db
        with db.conn() as c:
            rows = [(pd.Timestamp(r["day"]), (json.loads(r["data"]).get("vix") or {}).get("vix_prev"))
                    for r in c.execute("SELECT day, data FROM edge_signal ORDER BY day")]
        for i, (d, prev) in enumerate(rows):
            if not prev:
                continue
            before = rows[i - 1][0] if i > 0 else None
            key = before if before is not None and (d - before).days <= 4 else d - pd.Timedelta(days=1)
            out.setdefault(key, float(prev))                              # the close of the session before day d
    except Exception:
        pass
    try:
        from ... import edge
        s = edge.vix_close_saved()
        if s and s.get("close"):
            out.setdefault(pd.Timestamp(s["day"]), float(s["close"]))
    except Exception:
        pass
    s = pd.Series(out, dtype="float64")
    s.index = pd.DatetimeIndex(s.index)                                  # empty dict → still a date index
    return s.sort_index()


def _dte(d: pd.Timestamp) -> float:
    try:
        from ... import options
        e, _ = options.expiry_for("NIFTY", d + pd.Timedelta(hours=10))
        return float((e - d.date()).days)
    except Exception:
        return 3.0


class Sideways(Indicator):
    id, name, overlay = "sideways", "Sideways meter", False
    description = ("Chance that today stays quiet (small range) — from 09:45, every bar — and the day's likely high / low "
                   "limits on the price chart (80 % of days stay inside). Trained on NIFTY 50. Size, not direction.")
    warmup = 1600                                                   # ≥ 20 sessions of history for ADR5/ADR20
    params = [Param("warn", "float", 70, "Mark 'likely sideways' above %", 50, 95, 5),
              Param("trend", "float", 30, "Green 'room to move' below %", 5, 50, 5),
              Param("band", "bool", True, "Likely day high / low on the price chart (80 %)")]

    def compute(self, df, warn, trend, band):
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
        D["adr5_20"] = D.rng.rolling(5).mean().shift(1) / D.rng.rolling(20).mean().shift(1)
        vix = _vix_closes()
        allix = D.index.union(vix.index)
        vprev = vix.reindex(allix).ffill().shift(1).reindex(D.index)          # previous close, known before the open
        vchg = (vix / vix.shift(1) - 1).reindex(allix).ffill().shift(1).reindex(D.index)
        last_vix_day = {d: (vix.index[vix.index < d].max() if (vix.index < d).any() else None) for d in D.index}
        sym = df.attrs.get("symbol")
        G1 = {}
        try:
            if sym:
                m1 = store.bars(sym, "1")[["open", "high", "low", "close"]]
                m1 = m1[(m1.index >= df.index[0].normalize()) & (m1.index < df.index[-1] + pd.Timedelta(minutes=tfm))]
                G1 = {d: x for d, x in m1.groupby(m1.index.normalize())}
        except Exception:
            G1 = {}
        F, mean, scale = MODEL["features"], np.array(MODEL["mean"]), np.array(MODEL["scale"])
        coef, b0 = np.array(MODEL["quiet"]["coef"]), MODEL["quiet"]["intercept"]
        cal = MODEL["calibration"]
        n = len(df)
        prob, hi_lim, lo_lim = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
        last = None
        starts = np.r_[0, np.flatnonzero(np.diff(day.values.astype("int64"))) + 1, n]
        H, L, C = df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
        for a, b in zip(starts[:-1], starts[1:]):
            d = day[a]
            adr = D.at[d, "adr"]
            if not np.isfinite(adr) or adr <= 0 or not np.isfinite(D.at[d, "prev_range"]) or not np.isfinite(D.at[d, "adr5_20"]):
                continue
            o = D.at[d, "o"]
            vp = vprev.get(d)
            vsig = o * vp / 1588 / adr if vp is not None and np.isfinite(vp) else 1.0
            dte = _dte(d)
            x1 = G1.get(d)
            if x1 is not None and len(x1) >= 20:                          # inputs from 1-min bars, as in training
                t1 = (x1.index.hour * 60 + x1.index.minute).to_numpy()
                h1, l1, c1 = x1.high.to_numpy(), x1.low.to_numpy(), x1.close.to_numpy()
                o = float(x1.open.iloc[0])
            else:                                                         # fallback: the chart's own bars
                t1, h1, l1, c1 = tod[a:b] + tfm - 1, H[a:b], L[a:b], C[a:b]
            hm, lm = np.maximum.accumulate(h1), np.minimum.accumulate(l1)
            tw = np.cumsum(c1) / np.arange(1, len(c1) + 1)
            path = np.r_[0, np.cumsum(np.abs(np.diff(c1)))]
            for k in range(b - a):
                end_min = tod[a + k] + tfm - 555                          # minutes after 09:15 when this bar closes
                if end_min < 30 or end_min > 360:
                    continue
                j = int(np.searchsorted(t1, 555 + end_min)) - 1           # last 1-min bar that has closed by then
                if j < 5:
                    continue
                rs = hm[j] - lm[j]
                w0 = min(int(np.searchsorted(t1, 555 + end_min - 30)), j)
                feat = {"rng": rs / adr, "move": abs(c1[j] - o) / adr, "pos": (c1[j] - lm[j]) / rs if rs > 0 else 0.5,
                        "last30": (h1[w0:j + 1].max() - l1[w0:j + 1].min()) / adr, "elapsed": end_min / 375,
                        "prev_range": D.at[d, "prev_range"], "gap": D.at[d, "gap"],
                        "vix_chg": float(vchg.get(d)) if np.isfinite(vchg.get(d, np.nan)) else 0.0,
                        "dist_twap": abs(c1[j] - tw[j]) / adr, "eff": abs(c1[j] - o) / path[j] if path[j] > 0 else 0.0,
                        "vix_sig_adr": vsig, "oi_dte": dte, "adr5_20": D.at[d, "adr5_20"]}
                z = b0 + float(((np.array([feat[f] for f in F]) - mean) / scale) @ coef)
                p = 1 / (1 + math.exp(-z))
                prob[a + k] = 100 * p
                i = min(len(cal["edges"]) - 2, max(0, int(np.searchsorted(cal["edges"], p, side="right") - 1)))
                ext80 = cal["extra_range_p80_adr"][i] * adr
                hi_lim[a + k] = lm[j] + rs + ext80                        # if today's low holds, the high rarely passes this
                lo_lim[a + k] = hm[j] - rs - ext80                        # if today's high holds, the low rarely passes this
                last = (100 * p, adr, d, end_min, cal["extra_range_median_adr"][i] * adr, ext80, hi_lim[a + k], lo_lim[a + k],
                        last_vix_day.get(d), vp)
        cols = np.where(prob >= warn, "#f5a623", np.where(prob <= trend, "#26a69a", "#9e9e9e"))
        out = Output(series=[line("quiet", "Quiet-day chance %", df, prob, "#f5a623", pane=1, width=2),
                             line("warn", f"{warn:.0f}%", df, np.full(n, warn), "rgba(245,166,35,0.45)", pane=1, width=1),
                             line("trend", f"{trend:.0f}%", df, np.full(n, trend), "rgba(38,166,154,0.45)", pane=1, width=1),
                             line("half", "50%", df, np.full(n, 50.0), "rgba(120,123,134,0.4)", pane=1, width=1)])
        # the chart joins points across the night; hide that link: transparent on each day's first and last point
        edge = np.zeros(n, bool)
        ok = np.isfinite(prob)
        for a, b in zip(starts[:-1], starts[1:]):
            idx = np.flatnonzero(ok[a:b])
            if len(idx):
                edge[a + idx[0]] = edge[a + idx[-1]] = True
        out.series[0]["data"] = [dict(q, color="rgba(0,0,0,0)" if edge[i] else cols[i]) if "value" in q else q
                                 for i, q in enumerate(out.series[0]["data"])]
        if band:
            for sid, ttl, v, col in (("hi", "likely day high (80%)", hi_lim, "rgba(245,166,35,0.8)"),
                                     ("lo", "likely day low (80%)", lo_lim, "rgba(38,166,154,0.8)")):
                s = line(sid, ttl, df, v, col, pane=0, width=1)
                s["lineStyle"] = 2                                        # dashed
                s["data"] = [dict(q, color="rgba(0,0,0,0)") if ("value" in q and edge[i]) else q for i, q in enumerate(s["data"])]
                out.series.append(s)
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
            p, adr, d, end_min, ext_med, ext80, hl, ll, vday, vp = last
            hhmm = f"{(555 + end_min) // 60:02d}:{(555 + end_min) % 60:02d}"
            mood = "likely sideways" if p >= warn else "room to move" if p <= trend else "unclear"
            out.info = {"latest": f"{p:.0f}% quiet at {hhmm} · {mood}",
                        "range still to come": f"≈ {ext_med:.0f} pts typical (80% of days ≤ {ext80:.0f})",
                        "likely day limits": f"high ≤ {hl:,.0f} · low ≥ {ll:,.0f} (80%)",
                        "ADR": f"{adr:.0f}",
                        "model": f"v2 · NIFTY 50 · AUC {MODEL['auc_2022_26']['10:15']} at 10:15 (out of sample 2022–26)"}
            if vday is None or (d - vday).days > 4:
                out.info["VIX"] = f"⚠ India VIX close missing since {vday.date() if vday is not None else '—'} — using {vp}"
        return out


INDICATORS = [Sideways()]
