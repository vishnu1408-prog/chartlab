"""Backtest any indicator (built-in or custom) from its BUY / SELL markers (arrowUp = long, arrowDown = short).
Rules (intraday): entry at the close of the signal bar, entries 09:20–14:30, one trade at a time, max N per day,
stop / target in ATR(14) multiples or points, exit at 15:15 at the latest (never overnight), a bar touching both stop
and target counts as a stop, round-trip cost in % of price. Results in index points, R and ₹ for the given quantity."""
import numpy as np
import pandas as pd

from . import store
from .indicators import registry
from .indicators.base import rma, true_range


def run(ind_id, symbol, tf, start=None, end=None, params=None, sl=1.0, tp=2.0, unit="atr", max_per_day=2,
        entry_from="09:20", entry_to="14:30", qty=1, cost_pct=0.02, side="both") -> dict:
    ind = registry.get(ind_id)
    p = ind.resolve(params)
    df = store.bars(symbol, tf)
    if start is not None:
        i0 = int(df.index.searchsorted(store.from_epoch(start)))
        df = df.iloc[max(0, i0 - ind.warmup):]
    if end is not None:
        df = df[df.index <= store.from_epoch(end)]
    if len(df) > 120_000:
        raise ValueError("range too long for this timeframe — pick a shorter period or a higher timeframe")
    out = ind.compute(df, **p)
    t = store.to_epoch(df.index)
    pos_of = {int(x): i for i, x in enumerate(t)}
    atr = rma(true_range(df), 14).to_numpy()
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    tod = (df.index.hour * 60 + df.index.minute).to_numpy()
    day = df.index.normalize()
    hm = lambda s: int(s[:2]) * 60 + int(s[3:5])
    a0, a1 = hm(entry_from), hm(entry_to)
    signals = []
    for m in out.markers:
        d = 1 if m.get("shape") == "arrowUp" else -1 if m.get("shape") == "arrowDown" else 0
        i = pos_of.get(int(m.get("visible_from", m["time"])))
        if d == 0 or i is None or (start is not None and t[i] < start):
            continue
        if side == "long" and d < 0 or side == "short" and d > 0:
            continue
        signals.append((i, d))
    signals.sort()
    trades, busy_until, per_day = [], -1, {}
    for i, d in signals:
        if i <= busy_until or not (a0 <= tod[i] <= a1) or per_day.get(day[i], 0) >= max_per_day:
            continue
        risk = sl * atr[i] if unit == "atr" else sl
        reward = tp * atr[i] if unit == "atr" else tp
        if not np.isfinite(risk) or risk <= 0:
            continue
        en = c[i]
        stop, target = en - d * risk, (en + d * reward) if reward > 0 else None
        px, why, j = None, None, i
        for j in range(i + 1, len(c)):
            if day[j] != day[i]:                       # safety: never hold overnight
                px, why, j = c[j - 1], "day end", j - 1
                break
            if tod[j] >= 15 * 60 + 15:
                px, why = o[j], "15:15"
                break
            if (l[j] <= stop) if d > 0 else (h[j] >= stop):
                px, why = (min(o[j], stop) if d > 0 else max(o[j], stop)), "stop"
                break
            if target is not None and ((h[j] >= target) if d > 0 else (l[j] <= target)):
                px, why = (max(o[j], target) if d > 0 else min(o[j], target)), "target"
                break
        if px is None:
            px, why, j = c[-1], "end of data", len(c) - 1
        pts = (px - en) * d - cost_pct / 100 * en
        trades.append({"entry_time": int(t[i]), "exit_time": int(t[j]), "dir": d, "entry": round(float(en), 2), "exit": round(float(px), 2),
                       "pts": round(float(pts), 2), "R": round(float(pts / risk), 3), "rs": round(float(pts * qty), 0), "reason": why})
        busy_until = j
        per_day[day[i]] = per_day.get(day[i], 0) + 1
    return {"indicator": ind.name, "symbol": symbol, "tf": tf, "params": p, "trades": trades, "summary": summarize(trades, qty)}


def summarize(trades, qty=1) -> dict:
    if not trades:
        return {"trades": 0}
    pts = np.array([x["pts"] for x in trades])
    R = np.array([x["R"] for x in trades])
    eq = np.cumsum(pts)
    dd = float((eq - np.maximum.accumulate(np.r_[0, eq])[1:]).min())
    win, loss = pts[pts > 0].sum(), -pts[pts < 0].sum()
    years = {}
    for x in trades:
        y = str(store.from_epoch(x["entry_time"]).year)
        years.setdefault(y, []).append(x["pts"])
    days = len({store.from_epoch(x["entry_time"]).normalize() for x in trades})
    return {"trades": len(trades), "win_rate": round(float((pts > 0).mean() * 100), 1), "avg_pts": round(float(pts.mean()), 2),
            "total_pts": round(float(pts.sum()), 1), "total_rs": round(float(pts.sum() * qty)), "avg_R": round(float(R.mean()), 3),
            "profit_factor": round(float(win / loss), 2) if loss > 0 else None, "max_dd_pts": round(dd, 1),
            "avg_win": round(float(pts[pts > 0].mean()), 2) if (pts > 0).any() else 0, "avg_loss": round(float(pts[pts < 0].mean()), 2) if (pts < 0).any() else 0,
            "trades_per_day": round(len(trades) / max(days, 1), 2),
            "t_stat": round(float(pts.mean() / pts.std() * np.sqrt(len(pts))), 2) if len(pts) > 2 and pts.std() > 0 else None,
            "by_year": {y: {"n": len(v), "avg_pts": round(float(np.mean(v)), 2), "total": round(float(np.sum(v)), 1)} for y, v in sorted(years.items())},
            "equity": [round(float(x), 1) for x in eq]}
