"""Auto Fibonacci for the day — draws the retracement (and optional extensions) of a fixed range on every session:
  Previous day   : yesterday's high / low (drawn on today from the open)
  Previous week  : last calendar week's high / low
  First hour     : today's first 60 minutes (drawn from the end of that hour — nothing is known before it)
Direction follows the anchor range: an UP range (close above open) measures the pull-back from its HIGH (0 % = high,
100 % = low), a DOWN range from its LOW. Levels are fixed when they are drawn (no repainting; replay-safe).
This is a drawing tool, not a tested strategy — the 50 % of the previous day was backtested on its own (mid_level.py)
and touching it gave no edge.
"""
import numpy as np
import pandas as pd

from ... import store
from ..base import Indicator, Output, Param, times
from .levels import seg

RET = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
EXT = [1.272, 1.618, -0.272, -0.618]
COL = {0.0: "#9e9e9e", 1.0: "#9e9e9e", 0.236: "#ef5350", 0.382: "#ff9800", 0.5: "#ab47bc", 0.618: "#26a69a",
       0.786: "#2962ff"}


def session_key(idx: pd.DatetimeIndex, start_min: int) -> pd.DatetimeIndex:
    """trading day of each bar (24-hour markets start at 05:30 IST, so their day is shifted)"""
    return (idx - pd.Timedelta(minutes=start_min if start_min < 540 else 0)).normalize()


def fib_levels(hi: float, lo: float, up: bool, ext: bool) -> list:
    """[(ratio, price)] — up range: 0 % at the high, 100 % at the low. Pure."""
    rng = hi - lo
    out = []
    for r in RET + (EXT if ext else []):
        out.append((r, hi - r * rng if up else lo + r * rng))
    return out


def anchors(df: pd.DataFrame, anchor: str, start_min: int) -> list:
    """[(day, hi, lo, up, i_from, i_to)] — bar positions where each day's levels are drawn. Pure."""
    if df.empty:
        return []
    key = session_key(df.index, start_min)
    days = list(dict.fromkeys(key))
    pos = np.arange(len(df))
    out = []
    if anchor == "First hour":
        for d in days:
            ii = pos[key == d]
            t0 = df.index[ii[0]]
            fh = ii[df.index[ii] < t0 + pd.Timedelta(minutes=60)]
            rest = ii[df.index[ii] >= t0 + pd.Timedelta(minutes=60)]
            if not len(fh) or not len(rest):
                continue
            x = df.iloc[fh]
            out.append((d, float(x.high.max()), float(x.low.min()), bool(x.close.iloc[-1] >= x.open.iloc[0]),
                        int(rest[0]), int(ii[-1])))
        return out
    D = df.groupby(key).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    if anchor == "Previous week":
        wk = D.index.to_period("W")
        W = D.groupby(wk).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
        for d in days:
            pw = d.to_period("W") - 1
            if pw not in W.index:
                continue
            w = W.loc[pw]
            ii = pos[key == d]
            out.append((d, float(w.high), float(w.low), bool(w.close >= w.open), int(ii[0]), int(ii[-1])))
        return out
    for k in range(1, len(days)):                         # previous day
        d, p = days[k], days[k - 1]
        if (d - p).days > 5:
            continue
        r = D.loc[p]
        ii = pos[key == d]
        out.append((d, float(r.high), float(r.low), bool(r.close >= r.open), int(ii[0]), int(ii[-1])))
    return out


class FibDay(Indicator):
    id, name = "fib_day", "Auto Fibonacci (day)"
    description = ("Draws Fibonacci retracement levels for each day automatically — from the previous day's high/low "
                   "(default), the previous week, or today's first hour. 0 % sits at the end of the range's move, so "
                   "an up day measures pull-backs from its high. Golden zone 50–61.8 % shaded. Drawing tool, not a signal.")
    params = [Param("anchor", "choice", "Previous day", "Range", options=["Previous day", "Previous week", "First hour"]),
              Param("extensions", "bool", True, "Extensions 127.2 / 161.8 % (and −27.2 / −61.8 %)"),
              Param("zone", "bool", True, "Shade the golden zone (50–61.8 %)"),
              Param("history", "choice", "All days", "Draw on", options=["All days", "Today only"]),
              Param("labels", "bool", True, "Price labels")]

    def compute(self, df, anchor, extensions, zone, history, labels):
        if df.empty:
            return Output()
        step = np.diff(df.index.values).astype("timedelta64[m]").astype(int)
        if len(step) and int(np.median(step[step > 0])) >= 1440:
            return Output(info={"note": "For intraday charts (1-minute to 1-hour)."})
        sym = df.attrs.get("symbol")
        start_min = store.sess(sym)[0] if sym else 555
        A = anchors(df, anchor, start_min)
        if history == "Today only" and A:
            A = A[-1:]
        t = times(df)
        shapes = []
        for d, hi, lo, up, a, b in A:
            if hi <= lo:
                continue
            lv = fib_levels(hi, lo, up, bool(extensions))
            vis = int(t[a])
            if zone:
                p50, p618 = dict(lv)[0.5], dict(lv)[0.618]
                shapes.append({"kind": "box", "points": [{"time": int(t[a]), "price": float(max(p50, p618))},
                                                         {"time": int(t[b]), "price": float(min(p50, p618))}],
                               "color": "#26a69a33", "fill": "#26a69a1a", "width": 1, "visible_from": vis, "clip": True})
            for r, price in lv:
                ext = r < 0 or r > 1
                col = COL.get(r, "#78909c")
                txt = f"{r * 100:g}%" + (f" {price:,.2f}" if labels else "")
                shapes.append(seg(t[a], t[b], price, col, txt, 2 if r in (0.5, 0.618) else 1, 2 if ext else 0, vis))
        info = {"range": f"{anchor} · {'up → measured from the high' if A and A[-1][3] else 'down → measured from the low'}"
                if A else "not enough data"}
        if A:
            d, hi, lo, up, *_ = A[-1]
            lv = fib_levels(hi, lo, up, bool(extensions))
            info["latest"] = f"{d:%d %b}: high {hi:,.2f} · low {lo:,.2f} · " + " · ".join(
                f"{r * 100:g}% {p:,.2f}" for r, p in lv if 0 <= r <= 1)
        return Output(shapes=shapes, info=info)


INDICATORS = [FibDay()]
