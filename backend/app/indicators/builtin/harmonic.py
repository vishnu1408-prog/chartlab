"""Harmonic Patterns — the same engine as harmonic_patterns.pine (Python mirror from the research project).
Draws every pattern that produced a trade (XABCD + ratios, PRZ, entry/stop/T1, result), the open trade, and — in live
mode only — patterns still forming. Defaults = your chart settings + v5 (intraday, max 2/day, don't fight the day,
skip lunch, T1 = 0.5 AD then runner to 15:15). Backtest note: works on 15m, ≈ no edge on 5m after costs."""
import threading

import numpy as np
import pandas as pd

from ..base import Indicator, Output, Param, times
from ..lib import harmonic_ref as hr

PAT_KEYS = ["gartley", "bat", "altbat", "butterfly", "crab", "deepcrab", "shark", "cypher", "abcd"]
YOURS = {"bat", "butterfly", "crab", "abcd"}
BULL, BEAR = "#26a69a", "#ef5350"
_RUN_LOCK = threading.Lock()          # hr.run keeps open/pending trades on the function object


def build_ctx(d: pd.DataFrame) -> dict:
    idx = d.index
    day = idx.normalize()
    daily = d.groupby(day).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))
    prev = daily.shift(1)
    adr = (daily.high - daily.low).rolling(10).mean().shift(1)
    m = lambda s: s.reindex(day).to_numpy()
    return dict(tod=(idx.hour * 60 + idx.minute).tolist(), day=day.tolist(), dayOpen=m(daily.open).tolist(),
                pdh=m(prev.high).tolist(), pdl=m(prev.low).tolist(), pdc=m(prev.close).tolist(),
                adr=np.nan_to_num(m(adr), nan=1e9).tolist())


class Harmonic(Indicator):
    id, name = "harmonic", "Harmonic Patterns"
    description = __doc__.split("\n")[0]
    warmup = 600
    params = ([Param(k, "bool", k in YOURS, hr.NAMES[i], group="Patterns") for i, k in enumerate(PAT_KEYS)] + [
        Param("pivLen", "int", 3, "Pivot length", 2, 30, group="Zigzag"),
        Param("tol", "float", 8.0, "Ratio tolerance %", 0, 20, 0.5, group="Zigzag"),
        Param("confMode", "choice", "Reversal candle", "Trigger", options=["Touch PRZ", "Reversal close", "Reversal candle", "Reversal + RSI"], group="Entry"),
        Param("intraday", "bool", True, "Intraday (09:30–14:30 entries, square-off 15:15)", group="Entry"),
        Param("maxPerDay", "int", 2, "Max signals per day", 1, 10, group="Entry"),
        Param("dayFilter", "bool", True, "Don't fight the day", group="Entry"),
        Param("skipLunch", "bool", True, "Skip lunch 11:30–13:30", group="Entry"),
        Param("t1Fib", "float", 0.5, "Target 1 (retrace of AD)", 0.1, 2, 0.01, group="Exits"),
        Param("runner", "bool", True, "Second half: runner to square-off (else T2)", group="Exits"),
        Param("t2Fib", "float", 0.786, "Target 2 (retrace of AD)", 0.1, 3, 0.01, group="Exits"),
        Param("costPct", "float", 0.02, "Round-trip cost % (stats)", 0, 1, 0.005, group="Exits"),
        Param("ratios", "bool", True, "Show ratios", group="Display"),
        Param("forming", "bool", True, "Show forming PRZ (live only)", group="Display"),
    ])

    def compute(self, df, **p):
        if len(df) < 20:
            return Output()
        o, h, l, c = (df[k].tolist() for k in ("open", "high", "low", "close"))
        prm = hr.Params(pivLen=p["pivLen"], tol=p["tol"] / 100, confMode=p["confMode"],
                        enabled=tuple(bool(p[k]) for k in PAT_KEYS), t1Fib=p["t1Fib"], t2Fib=p["t2Fib"],
                        noT2=p["runner"], stretchMax=0.25 if p["dayFilter"] else None)
        if p["intraday"]:
            prm.intraday, prm.maxPerDay, prm.minGapBars, prm.oneAtATime = True, p["maxPerDay"], 3, True
            prm.noEntryWin = (690, 810) if p["skipLunch"] else None
        else:
            prm.maxPerDay = p["maxPerDay"]
        with _RUN_LOCK:
            _, trades, _ = hr.run(o, h, l, c, prm, ctx=build_ctx(df))
            open_, pending = list(getattr(hr.run, "open", [])), list(getattr(hr.run, "pending", []))
        t = times(df)
        out = Output()
        res = []
        for s in trades:
            self._draw(out, s, t, p, closed=True)
            res.append(s.r - p["costPct"] / 100 * s.entry / max(s.risk, 1e-9))
        for s in open_:
            self._draw(out, s, t, p, closed=False)
        if p["forming"]:
            for s in pending:
                col = BULL if s.dir == 1 else BEAR
                out.shapes.append({"kind": "box", "points": [{"time": int(t[s.cBar]), "price": float(s.top)},
                                                             {"time": int(t[-1]), "price": float(s.bot)}],
                                   "color": col, "fill": col + "22", "width": 1, "dash": 2,
                                   "text": f"forming {hr.NAMES[s.pid]} PRZ", "visible_from": int(t[min(s.created, len(t) - 1)]),
                                   "live_only": True})
        if res:
            r = np.array(res)
            out.info = {"trades": str(len(r)), "win rate": f"{100 * (r > 0).mean():.0f}%", "avg R (after costs)": f"{r.mean():+.2f}",
                        "total R": f"{r.sum():+.1f}", "open trade": "yes" if open_ else "no"}
        return out

    @staticmethod
    def _draw(out, s, t, p, closed):
        col = BULL if s.dir == 1 else BEAR
        n = len(t) - 1
        vis = int(t[s.entryBar])
        bars = [s.xBar, s.aBar, s.bBar, s.cBar, min(s.dBar, n)]
        prices = [s.x, s.a, s.b, s.c, s.d]
        pts = [{"time": int(t[b]), "price": float(v)} for b, v in zip(bars, prices)]
        out.shapes.append({"kind": "polygon", "points": pts[:3], "color": col, "fill": col + "22", "width": 1, "visible_from": vis})
        out.shapes.append({"kind": "polygon", "points": pts[2:], "color": col, "fill": col + "22", "width": 1, "visible_from": vis})
        out.shapes.append({"kind": "polyline", "points": pts, "color": col, "width": 2, "visible_from": vis})
        for name, pt in zip("XABCD", pts):
            out.shapes.append({"kind": "label", "points": [pt], "text": name, "color": col, "visible_from": vis})
        if p["ratios"]:
            xa = abs(s.a - s.x) or 1e-9
            ab = abs(s.b - s.a) or 1e-9
            bc = abs(s.c - s.b) or 1e-9
            for (i, j), txt in (((0, 2), f"{ab / xa:.3f}"), ((1, 3), f"{bc / ab:.3f}"), ((2, 4), f"{abs(s.d - s.c) / bc:.3f}"),
                                ((0, 4), f"{abs(s.d - s.a) / xa:.3f}")):
                mid = {"time": int((pts[i]["time"] + pts[j]["time"]) // 2), "price": (pts[i]["price"] + pts[j]["price"]) / 2}
                out.shapes.append({"kind": "label", "points": [mid], "text": txt, "color": "#b2b5be", "small": True, "visible_from": vis})
        end = int(t[min(s.exitBar, n)]) if closed and s.exitBar else int(t[n])
        out.shapes.append({"kind": "box", "points": [{"time": int(t[s.cBar]), "price": float(s.top)}, {"time": vis, "price": float(s.bot)}],
                           "color": col, "fill": col + "18", "width": 1, "dash": 2, "visible_from": vis})
        for price, colr, txt in ((s.entry, "#d1d4dc", "Entry"), (s.entry - s.dir * s.risk, "#ef5350", "SL"), (s.t1, "#26a69a", "T1")):
            out.shapes.append({"kind": "segment", "points": [{"time": vis, "price": float(price)}, {"time": end, "price": float(price)}],
                               "color": colr, "width": 1, "dash": 1 if txt != "Entry" else 0, "text": txt, "visible_from": vis, "clip": True})
        out.markers.append({"time": vis, "position": "belowBar" if s.dir == 1 else "aboveBar",
                            "shape": "arrowUp" if s.dir == 1 else "arrowDown", "color": col,
                            "text": f"{'BUY' if s.dir == 1 else 'SELL'} {hr.NAMES[s.pid]}", "visible_from": vis})
        if closed:
            out.markers.append({"time": end, "position": "aboveBar" if s.dir == 1 else "belowBar", "shape": "circle",
                                "color": "#26a69a" if s.r > 0 else "#ef5350", "text": f"{s.result} {s.r:+.2f}R", "visible_from": end})


INDICATORS = [Harmonic()]
