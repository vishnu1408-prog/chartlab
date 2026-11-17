"""Volume tools: session volume profile (histogram + POC / value area) and VWAP with standard-deviation bands.

Volume comes from the chart's own bars: real traded volume for MCX futures, stocks and BTC. NSE indices (NIFTY,
SENSEX…) publish no volume, so there the profile counts TIME at each price (Market Profile / TPO) and the VWAP becomes a
TWAP — both labelled. A day's profile is drawn only once its bars exist (replay never shows a finished profile early).
"""
import threading
import time

import numpy as np
import pandas as pd

from ..base import Indicator, Output, Param, line, times
from .levels import seg


def bin_profile(h: np.ndarray, l: np.ndarray, w: np.ndarray, bins: int) -> tuple:
    """Spread each bar's weight evenly over the price bins it covers. Returns (bin edges, weight per bin)."""
    lo, hi = float(np.min(l)), float(np.max(h))
    if hi <= lo:
        hi = lo + 1e-9
    edges = np.linspace(lo, hi, bins + 1)
    out = np.zeros(bins)
    for a, b, v in zip(l, h, w):
        i0 = min(int((a - lo) / (hi - lo) * bins), bins - 1)
        i1 = min(int((b - lo) / (hi - lo) * bins), bins - 1)
        out[i0:i1 + 1] += v / (i1 - i0 + 1)
    return edges, out


def value_area(edges: np.ndarray, vol: np.ndarray, share: float = 0.70) -> tuple:
    """POC and the value area (grow from the POC towards the bigger neighbour until `share` of the weight)."""
    poc = int(np.argmax(vol))
    lo = hi = poc
    total, got = vol.sum(), vol[poc]
    while got < share * total and (lo > 0 or hi < len(vol) - 1):
        up = vol[hi + 1] if hi < len(vol) - 1 else -1
        dn = vol[lo - 1] if lo > 0 else -1
        if up >= dn:
            hi += 1
            got += up
        else:
            lo -= 1
            got += dn
    mid = (edges[:-1] + edges[1:]) / 2
    return mid[poc], edges[hi + 1], edges[lo]


class SessionProfile(Indicator):
    id, name = "vp_session", "Volume Profile (session)"
    description = ("Each day's volume at price drawn as horizontal bars, with POC and the 70 % value area; the levels "
                   "extend into the next day. Indices (no volume) use time at price (TPO).")
    warmup = 0
    params = [Param("days", "int", 5, "Days to draw", 1, 30), Param("bins", "int", 40, "Price rows", 10, 150),
              Param("width", "float", 0.35, "Bar length (share of the day)", 0.1, 1.0, 0.05),
              Param("extend", "bool", True, "Extend POC / VAH / VAL into the next day"),
              Param("color", "color", "#5c6bc0", "Bar colour")]

    def compute(self, df, days, bins, width, extend, color):
        if df.empty:
            return Output()
        use_vol = float(df.volume.sum()) > 0 if "volume" in df else False
        t = times(df)
        idx = pd.Series(np.arange(len(df)), index=df.index).groupby(df.index.normalize())
        groups = [(d, g.to_numpy()) for d, g in idx]
        groups = groups[-(days + 1):]
        shapes = []
        last_levels = None
        for k, (d, ix) in enumerate(groups):
            h, l = df.high.to_numpy()[ix], df.low.to_numpy()[ix]
            w = df.volume.to_numpy()[ix] if use_vol else np.ones(len(ix))
            if w.sum() <= 0:
                continue
            edges, vol = bin_profile(h, l, w, bins)
            poc, vah, val = value_area(edges, vol)
            t0, t1, vis = int(t[ix[0]]), int(t[ix[-1]]), int(t[ix[-1]])
            top = vol.max()
            in_va = lambda p: val <= p <= vah
            if k > 0 or len(groups) == 1:                     # draw the days asked for (skip the extra lead-in day)
                for i, v in enumerate(vol):
                    if v <= 0:
                        continue
                    p = (edges[i] + edges[i + 1]) / 2
                    j = ix[min(len(ix) - 1, max(1, int(round((len(ix) - 1) * width * v / top))))]
                    col = color if in_va(p) else "#78909c"
                    s = seg(t0, int(t[j]), p, col, "", 3, 0, vis)       # ends on a real bar (the chart needs that)
                    shapes.append(s)
                shapes.append(seg(t0, t1, poc, "#ffeb3b", "POC", 2, 0, vis))
                shapes.append(seg(t0, t1, vah, "#90a4ae", "VAH", 1, 2, vis))
                shapes.append(seg(t0, t1, val, "#90a4ae", "VAL", 1, 2, vis))
            if extend and last_levels is not None:            # yesterday's levels drawn across today
                pp, ph, pl = last_levels
                shapes += [seg(t0, t1, pp, "#ffeb3b", "pPOC", 1, 2, t0), seg(t0, t1, ph, "#b0bec5", "pVAH", 1, 3, t0),
                           seg(t0, t1, pl, "#b0bec5", "pVAL", 1, 3, t0)]
            last_levels = (poc, vah, val)
        info = {"mode": "volume at price" if use_vol else "time at price (TPO) — this market publishes no volume"}
        if last_levels:
            info["today (so far)"] = f"POC {last_levels[0]:,.2f} · VAH {last_levels[1]:,.2f} · VAL {last_levels[2]:,.2f}"
        return Output(shapes=shapes, info=info)


class VWAPBands(Indicator):
    id, name = "vwap_bands", "VWAP + bands"
    warmup = 4000                                          # a month of 5-minute bars for the week / month anchors
    description = ("Volume-weighted average price with ±1σ / ±2σ bands, anchored to the session, week or month. "
                   "Indices (no volume) get a TWAP (time-weighted) instead.")
    params = [Param("anchor", "choice", "session", "Anchor", options=["session", "week", "month"]),
              Param("band1", "float", 1.0, "Band 1 (σ)", 0.0, 5, 0.1), Param("band2", "float", 2.0, "Band 2 (σ)", 0.0, 5, 0.1),
              Param("color", "color", "#e040fb", "VWAP colour")]

    def compute(self, df, anchor, band1, band2, color):
        if df.empty:
            return Output()
        use_vol = float(df.volume.sum()) > 0 if "volume" in df else False
        w = df.volume.astype(float) if use_vol else pd.Series(1.0, index=df.index)
        tp = (df.high + df.low + df.close) / 3
        key = {"session": df.index.normalize(), "week": df.index.to_period("W").start_time,
               "month": df.index.to_period("M").start_time}[anchor]
        cw = w.groupby(key).cumsum().replace(0, np.nan)
        vwap = (tp * w).groupby(key).cumsum() / cw
        var = ((tp ** 2) * w).groupby(key).cumsum() / cw - vwap ** 2
        sd = np.sqrt(var.clip(lower=0))
        name = "VWAP" if use_vol else "TWAP"
        series = [line("vwap", f"{name} ({anchor})", df, vwap.to_numpy(), color)]
        for k, b in (("1", band1), ("2", band2)):
            if b > 0:
                series.append(line(f"up{k}", f"+{b:g}σ", df, (vwap + b * sd).to_numpy(), "#9575cd", width=1))
                series.append(line(f"dn{k}", f"−{b:g}σ", df, (vwap - b * sd).to_numpy(), "#9575cd", width=1))
        p, v, s_ = float(df.close.iloc[-1]), float(vwap.iloc[-1]), float(sd.iloc[-1])
        info = {"mode": "volume-weighted" if use_vol else "time-weighted (TWAP) — this market publishes no volume"}
        if np.isfinite(v) and s_ > 0:
            info["now"] = f"{name} {v:,.2f} · price {(p - v) / s_:+.2f}σ from it"
        return Output(series=series, info=info)


_fp_busy: dict = {}


class Footprint(Indicator):
    id, name = "footprint", "Footprint (order flow)"
    cache = False                                          # trades keep arriving in the background
    description = ("Inside every candle: volume SOLD into the bid × BOUGHT from the ask at each price, imbalances "
                   "(≥ 3× diagonal) highlighted, the candle's POC boxed and its delta (buy − sell) below. Real trade "
                   "sides for BTC (Binance); NIFTY futures / MCX from ChartLab's live recording.")
    warmup = 0
    params = [Param("level", "float", 0.0, "Price step (0 = auto)", 0.0, 100000, 0.05),
              Param("max_bars", "int", 150, "Candles with a footprint", 10, 600),
              Param("imbalance", "float", 3.0, "Imbalance ratio", 1.5, 10, 0.5),
              Param("mode", "choice", "auto", "Show", options=["auto", "numbers", "heat"])]

    def compute(self, df, level, max_bars, imbalance, mode):
        from ... import footprint as fp
        sym = df.attrs.get("symbol")
        if df.empty:
            return Output()
        if sym not in ("BTCUSD",) and not fp.has_data(sym):
            return Output(info={"note": "No footprint data for this market yet. BTC: real Binance trades. NIFTY "
                                        "futures / MCX: recorded live by ChartLab from the next session."})
        step = np.diff(df.index.values[-50:]).astype("timedelta64[m]").astype(int)
        tf_min = int(np.median(step[step > 0])) if len(step) else 1
        bars = df.iloc[-max_bars:]
        if not level:                                     # ~12 rows across a typical candle, rounded to 1-2-5
            rng = float((bars.high - bars.low).median()) / 12 or 1.0
            mag = 10 ** np.floor(np.log10(rng))
            level = float(min((m * mag for m in (1, 2, 5, 10)), key=lambda v: abs(v - rng)))
            level = max(level, fp.BIN.get(sym, 0.05))
        start, end = bars.index[0], bars.index[-1] + pd.Timedelta(minutes=tf_min)
        note = None
        if sym == "BTCUSD":
            key = (sym, start.floor("h"))
            if _fp_busy.get(key, 0) < time.time() - 20:          # at most one download every 20 s per range
                _fp_busy[key] = time.time()
                threading.Thread(target=fp.ensure_btc, args=(start.to_pydatetime(), end.to_pydatetime()), daemon=True).start()
            note = "BTC trades from Binance (updates every candle)"
        rows = fp.load(sym, start, end)
        fc = fp.candles(rows, bars.index, tf_min, level)
        t = times(bars)
        tpos = {ts: int(x) for ts, x in zip(bars.index, t)}
        shapes = [{"kind": "footprint", "time": tpos[b], "points": [{"time": tpos[b], "price": v["poc"]}],
                   "rows": v["rows"], "level": level, "delta": v["delta"], "poc": v["poc"], "imb": imbalance,
                   "mode": mode, "visible_from": tpos[b], "color": "#888"} for b, v in fc.items() if b in tpos]
        info = {"price step": f"{level:g}", "candles with data": f"{len(shapes)} of {len(bars)}"}
        if shapes:
            d = sum(s["delta"] for s in shapes)
            info["delta (shown candles)"] = f"{d:+,.2f}"
        if note:
            info["source"] = note
        if not shapes:
            info["status"] = "loading trades… (first time can take ~1 minute)" if sym == "BTCUSD" else "no data yet"
        return Output(shapes=shapes, info=info)


INDICATORS = [SessionProfile(), VWAPBands(), Footprint()]
