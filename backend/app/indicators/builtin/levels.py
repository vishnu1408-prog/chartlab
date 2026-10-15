"""Day-level indicators: CPR / pivots + previous-day high/low, Opening Range, previous-day volume profile.
Each day's levels become visible at the first bar of the day (they only use earlier data)."""
import numpy as np
import pandas as pd

from ..base import Indicator, Output, Param, times


def _days(df):
    day = df.index.normalize()
    t = times(df)
    first = pd.Series(t, index=df.index).groupby(day).first()
    last = pd.Series(t, index=df.index).groupby(day).last()
    d = df.groupby(day).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    return d, first, last


def seg(t0, t1, price, color, text="", width=1, dash=0, vis=None):
    return {"kind": "segment", "points": [{"time": int(t0), "price": float(price)}, {"time": int(t1), "price": float(price)}],
            "color": color, "width": width, "dash": dash, "text": text, "visible_from": int(vis if vis is not None else t0),
            "clip": True}


class Pivots(Indicator):
    id, name = "pivots", "CPR + Pivots + Prev-day H/L"
    description = "Central Pivot Range, classic R1/S1 (R2/S2 optional) and previous day high/low, from yesterday's OHLC."
    params = [Param("show_r2", "bool", False, "Show R2 / S2"), Param("show_pdhl", "bool", True, "Prev-day high / low")]

    def compute(self, df, show_r2, show_pdhl):
        d, first, last = _days(df)
        p = d.shift(1)
        P = (p.high + p.low + p.close) / 3
        BC = (p.high + p.low) / 2
        TC = 2 * P - BC
        shapes = []
        for day in d.index[1:]:
            if not np.isfinite(P[day]):
                continue
            t0, t1 = first[day], last[day]
            lv = [(max(TC[day], BC[day]), "#7e57c2", "TC"), (P[day], "#7e57c2", "P"), (min(TC[day], BC[day]), "#7e57c2", "BC"),
                  (2 * P[day] - p.low[day], "#ef5350", "R1"), (2 * P[day] - p.high[day], "#26a69a", "S1")]
            if show_r2:
                rng = p.high[day] - p.low[day]
                lv += [(P[day] + rng, "#ef5350", "R2"), (P[day] - rng, "#26a69a", "S2")]
            if show_pdhl:
                lv += [(p.high[day], "#ff9800", "PDH"), (p.low[day], "#ff9800", "PDL")]
            for price, col, txt in lv:
                shapes.append(seg(t0, t1, price, col, txt, dash=0 if txt in ("TC", "P", "BC") else 2))
        return Output(shapes=shapes)


class ORB(Indicator):
    id, name = "orb", "Opening Range (ORB)"
    description = "High/low of the first N minutes, extended to 15:15, with breakout markers (first close outside)."
    params = [Param("minutes", "int", 15, "Range minutes", 5, 120, 5), Param("markers", "bool", True, "Breakout markers")]

    def compute(self, df, minutes, markers):
        t = times(df)
        tod = df.index.hour * 60 + df.index.minute
        day = df.index.normalize()
        end = 555 + minutes
        shapes, mk = [], []
        for dd, idx in pd.Series(np.arange(len(df)), index=df.index).groupby(day):
            ii = idx.to_numpy()
            rng = ii[tod[ii] < end]
            if len(rng) == 0 or len(rng) == len(ii):
                continue
            # bars must lie fully inside the range window (a 30m bar starting 09:15 is not a 15-minute range)
            step = (tod[ii[1]] - tod[ii[0]]) if len(ii) > 1 else 1
            if tod[rng[-1]] + step > end:
                continue
            H, L = df.high.to_numpy()[rng].max(), df.low.to_numpy()[rng].min()
            vis = t[rng[-1]]
            t_end = t[ii[-1]]
            shapes += [seg(t[rng[0]], t_end, H, "#26a69a", f"ORH {minutes}m", 2, 0, vis),
                       seg(t[rng[0]], t_end, L, "#ef5350", f"ORL {minutes}m", 2, 0, vis)]
            if markers:
                c = df.close.to_numpy()
                for k in ii[tod[ii] >= end]:
                    if c[k] > H or c[k] < L:
                        up = c[k] > H
                        mk.append({"time": int(t[k]), "position": "belowBar" if up else "aboveBar",
                                   "shape": "arrowUp" if up else "arrowDown", "color": "#26a69a" if up else "#ef5350",
                                   "text": "ORB ↑" if up else "ORB ↓", "visible_from": int(t[k])})
                        break
        return Output(shapes=shapes, markers=mk)


def profile(h, l, w, nbins=60, va=0.70):
    lo, hi = float(np.min(l)), float(np.max(h))
    if hi <= lo:
        return lo, lo, lo
    edges = np.linspace(lo, hi, nbins + 1)
    mid = (edges[:-1] + edges[1:]) / 2
    vol = np.zeros(nbins)
    for a, b, x in zip(l, h, w):
        m = (mid >= a) & (mid <= b)
        k = m.sum()
        if k:
            vol[m] += x / k
        else:
            vol[np.clip(np.searchsorted(edges, (a + b) / 2) - 1, 0, nbins - 1)] += x
    poc = int(np.argmax(vol))
    lo_i = hi_i = poc
    tot, acc = vol.sum(), vol[poc]
    while acc < va * tot and (lo_i > 0 or hi_i < nbins - 1):
        up = vol[hi_i + 1] if hi_i < nbins - 1 else -1
        dn = vol[lo_i - 1] if lo_i > 0 else -1
        if up >= dn:
            hi_i += 1; acc += up
        else:
            lo_i -= 1; acc += dn
    return mid[poc], mid[hi_i], mid[lo_i]


class VolumeProfile(Indicator):
    id, name = "vprofile", "Prev-day Volume Profile"
    description = "Yesterday's POC / VAH / VAL (70% value area). Indices have no volume → time-at-price (TPO) profile."
    params = [Param("bins", "int", 60, "Price bins", 10, 300)]

    def compute(self, df, bins):
        d, first, last = _days(df)
        use_vol = df.volume.sum() > 0
        lv = {}
        for day, g in df.groupby(df.index.normalize()):
            w = g.volume.to_numpy() if use_vol else np.ones(len(g))
            lv[day] = profile(g.high.to_numpy(), g.low.to_numpy(), w, bins)
        shapes = []
        days = list(d.index)
        for a, b in zip(days[:-1], days[1:]):
            poc, vah, val = lv[a]
            t0, t1 = first[b], last[b]
            shapes += [seg(t0, t1, poc, "#ffeb3b", "POC", 2), seg(t0, t1, vah, "#90a4ae", "VAH", 1, 2),
                       seg(t0, t1, val, "#90a4ae", "VAL", 1, 2)]
        return Output(shapes=shapes, info={"mode": "volume" if use_vol else "TPO (time at price)"})


INDICATORS = [Pivots(), ORB(), VolumeProfile()]
