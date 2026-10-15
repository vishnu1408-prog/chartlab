"""Daily Plan — Gann opening drive + Morning Edge, at most ONE NIFTY trade a day (~10–11 a month at trigger 0.10).

Order of the day (no look-ahead: each step only uses what is known at that time):
  1. Gann opening drive (gann_drive.py): strong 09:15 drive (body ≥ 0.25×ADR10) and a 5m close beyond Gann ±1 whose
     entry (next bar open) is BEFORE 09:45 → take it: target 35 / stop 35 / 15:15.
  2. Otherwise at 09:45, Morning Edge (edge.py) |prediction| ≥ trigger → BUY / SELL: stop 35, half at +35, the rest's
     stop to entry, 15:15.
  3. Otherwise a later Gann drive signal (up to 14:30) → take it (35/35).
Backtest after 0.02 % costs (indicators tests, 2026-10-13), NIFTY index pts:
  trigger 0.10: 2017–21 12.9/month, win 56 %, +4.3/trade · 2022–Oct26 10.5/month, win 56 %, +2.5–2.8, PF 1.16, worst DD
                447 pts, longest losing run 6; 9 of 10 years positive (2023 −1.0).
  trigger 0.15: 2022–Oct26 7.5/month, win 59 %, +4.6, PF 1.29; 10 of 10 years positive.
The combination rule was chosen after both parts were known, so the live tally (from 14 Oct 2026) is the real test.
"""
import numpy as np
import pandas as pd

from ... import edge, store
from ..base import Indicator, Output, Param, times, to_epoch
from .gann_drive import day_trades
from .levels import seg

LIVE_FROM = pd.Timestamp("2026-10-14")
BACKTEST = {"0.10": "2022–Oct26: 10.5/month (~2.5/week), win 56%, +2.5 NIFTY pts/trade, PF 1.16, worst DD 447 pts",
            "0.15": "2022–Oct26: 7.5/month, win 59%, +4.6 NIFTY pts/trade, PF 1.29, worst DD 410 pts"}


def _epoch(ts) -> int:
    return int(to_epoch(pd.DatetimeIndex([pd.Timestamp(ts)]))[0])


def plan(df5: pd.DataFrame, m1: pd.DataFrame | None, sig: pd.DataFrame, th: float) -> list:
    """one row per day that has a trade: which strategy, direction, entry time/price, exit legs, pts after costs"""
    gann = {r["day"]: r for r in day_trades(df5, m1) if r.get("strong") and "entry_i" in r}
    out = []
    for d in df5.index.normalize().unique():
        g = gann.get(d)
        t945 = d + pd.Timedelta(minutes=585)
        g_t = df5.index[g["entry_i"]] if g else None
        pred = float(sig.pred[d]) if d in sig.index and np.isfinite(sig.pred.get(d, np.nan)) else np.nan
        if g is not None and g_t < t945:
            pick = "gann"
        elif np.isfinite(pred) and abs(pred) >= th:
            pick = "me"
        elif g is not None:
            pick = "gann"
        else:
            continue
        if pick == "gann":
            row = {"day": d, "src": "Gann", "dir": g["dir"], "t": g_t, "entry": g["entry"], "done": "pts" in g}
            if "pts" in g:
                row.update(pts=g["pts"], legs=[(1.0, g["exit_t"], g["pts"], g["why"])])
            out.append(row)
            continue
        dirn = 1 if pred > 0 else -1
        row = {"day": d, "src": "ME", "dir": dirn, "t": t945, "pred": pred, "done": False}
        if m1 is not None and len(m1):
            day = m1[(m1.index >= t945) & (m1.index <= d + pd.Timedelta(minutes=915))]
        else:
            day = pd.DataFrame()
        if day.empty or day.index[0] != t945:
            day = df5[(df5.index >= t945) & (df5.index <= d + pd.Timedelta(minutes=915))]
        if day.empty or day.index[0] != t945:
            row["entry"] = None
            out.append(row)
            continue
        res = edge.e2_legs(day, dirn)
        row.update(entry=res["entry"], done=res["done"])
        if res.get("pts") is not None:
            cost = 0.0002 * res["entry"]
            row.update(pts=res["pts"] - (cost if res["done"] else 0), legs=res["legs"])
        out.append(row)
    return out


def tally(rows) -> str:
    p = np.array([r["pts"] for r in rows if r.get("done") and r.get("pts") is not None])
    if not len(p):
        return "no trades yet"
    return f"{len(p)} trades · win {round(100 * (p > 0).mean())}% · avg {p.mean():+.1f} NIFTY pts · total {p.sum():+.0f}"


class DailyPlan(Indicator):
    id, name = "daily_plan", "Daily Plan: Gann drive + Morning Edge"
    description = ("One NIFTY trade a day at most: an early Gann opening-drive signal, else the 09:45 Morning Edge "
                   "signal, else a later Gann signal. ~10–11 trades a month at trigger 0.10. Forward test.")
    warmup = 900
    params = [Param("trigger", "choice", "0.10", "Morning Edge trigger |pred| ≥", options=["0.10", "0.15"]),
              Param("show_lines", "bool", True, "Draw stop / target lines")]

    def compute(self, df, trigger, show_lines):
        th = float(trigger)
        if df.empty or len(df) < 100:
            return Output()
        sym = df.attrs.get("symbol")
        if sym and sym != edge.SYM:
            return Output(info={"note": "Built for NIFTY 50 only."})
        step = np.diff(df.index.values).astype("timedelta64[m]").astype(int)
        if int(np.median(step[step > 0])) != 5:
            return Output(info={"note": "Works on the 5-minute chart only."})
        try:
            m1 = store.bars(edge.SYM, "1")
            m1 = m1[(m1.index >= df.index[0]) & (m1.index < df.index[-1] + pd.Timedelta(minutes=5))]
        except Exception:
            m1 = None
        sig = edge.signals()
        rows = plan(df, m1, sig, th)
        t = times(df)
        bar_at = lambda ts: int(t[max(0, int(df.index.searchsorted(pd.Timestamp(ts), side="right")) - 1)])
        markers, shapes = [], []
        for r in rows:
            d, dirn = r["day"], r["dir"]
            x0 = bar_at(r["t"] - pd.Timedelta(minutes=5)) if r["src"] == "Gann" else bar_at(r["t"])
            lab = "BUY" if dirn == 1 else "SELL"
            extra = f" {r['pred']:+.2f}" if r["src"] == "ME" else ""
            markers.append({"time": x0, "position": "belowBar" if dirn == 1 else "aboveBar",
                            "shape": "arrowUp" if dirn == 1 else "arrowDown",
                            "color": "#26a69a" if dirn == 1 else "#ef5350", "visible_from": _epoch(r["t"]),
                            "text": f"Plan {lab} ({r['src']}{extra})"})
            if r.get("entry") is None:
                continue
            e = r["entry"]
            if show_lines:
                end = bar_at(d + pd.Timedelta(minutes=915))
                xs = bar_at(r["t"])
                shapes.append(seg(xs, end, e - dirn * 35, "#ef5350", "SL", 1, 1, xs))
                shapes.append(seg(xs, end, e + dirn * 35, "#26a69a", "+35", 1, 1, xs))
            for frac, when, pts, why in r.get("legs", []):
                markers.append({"time": bar_at(when), "position": "aboveBar" if dirn == 1 else "belowBar",
                                "shape": "square", "color": "#26a69a" if pts > 0 else ("#ef5350" if pts < 0 else "#78909c"),
                                "visible_from": max(_epoch(when), bar_at(when)),
                                "text": f"{'✓' if pts > 0 else '✗' if pts < 0 else '•'} {why} {pts:+.0f}{' (½)' if frac < 1 else ''}"})
        info = {"rules": f"1 trade/day: Gann drive before 09:45 → else Morning Edge 09:45 (|pred| ≥ {th:.2f}) → else later "
                         f"Gann · stop 35 · target 35 (ME: ½ at +35, rest to breakeven) · 15:15 out",
                "backtest": BACKTEST.get(trigger, ""),
                "on this chart": tally(rows),
                "live since 14 Oct 2026": tally([r for r in rows if r["day"] >= LIVE_FROM])}
        today = df.index[-1].normalize()
        tr = [r for r in rows if r["day"] == today]
        if tr:
            r = tr[0]
            st = f"{'BUY' if r['dir'] == 1 else 'SELL'} via {r['src']} at {r['t']:%H:%M}"
            if r.get("done"):
                st += f" · closed {r['pts']:+.1f} pts"
            elif r.get("entry") is not None:
                st += f" · open from {r['entry']:.1f}"
            info["today"] = st
        elif df.index[-1] >= today + pd.Timedelta(minutes=555):
            info["today"] = ("waiting: Gann signal before 09:45 or Morning Edge at 09:45"
                             if df.index[-1] < today + pd.Timedelta(minutes=585)
                             else "no trade yet (a later Gann drive signal can still come until 14:30)")
        return Output(shapes=shapes, markers=markers, info=info)


INDICATORS = [DailyPlan()]
