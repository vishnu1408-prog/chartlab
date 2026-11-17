"""NG FVG (MCX natural gas, 5-minute) — a FORWARD TEST, not a proven strategy.

Rule (indicators/tests/fvg_markets.py / fvg_ng_trail.py "tight · M0 hold", pre-registered 2026-10-23):
  gap    : bullish FVG when low[k] > high[k-2], gap ≥ 0.1 ATR14 and the middle candle's body ≥ 0.8 ATR (displacement);
           bearish mirror. Known at the close of bar k; valid for 24 bars.
  entry  : limit at the gap edge (bull: low[k], bear: high[k]) on the first pull-back, entries 09:30–22:00
           (a bar opening beyond the edge fills at its open)
  stop   : other side of the gap ∓ 0.1 ATR (tight). No target, no move to cost, no trail — hold to 23:00.
           (Moving to cost / trailing was tested and made it worse: it cuts the trend days that pay for everything.)
  limits : one trade at a time, max 2 a day; the fill bar can stop the trade (conservative).
Backtest, full lot 1,250 mmBtu, charges 0.03 %: 2016–2025 ≈ +₹8,800 / month, no losing year — BUT ~38 trades a month,
~1 in 4 wins, losing streaks of 20+, and with 0.2 pts slippage per trade (₹250) it is ≈ −₹700 / month (6 losing years).
Judge it only on a paper forward test with real fills.
"""
import numpy as np
import pandas as pd

from ..base import Indicator, Output, Param, times, to_epoch
from .levels import seg

SYM = "MCX NATURALGAS"
E0, E1, EX = 570, 1320, 1380                               # entries 09:30–22:00, exit 23:00 (minutes of the IST day)
COST_PCT = 0.03
LOT = 1250
LIVE_FROM = pd.Timestamp("2026-10-23")
BACKTEST = ("2016–25, full lot, charges only: +₹8,800/month, 0 losing years · with 0.2 pt slippage: −₹700/month, "
            "6 losing years · ~38 trades/month, ~25 % win, streaks of 20+ losses")


def atr14(df: pd.DataFrame) -> np.ndarray:
    pc = df.close.shift(1)
    tr = np.maximum(df.high - df.low, np.maximum((df.high - pc).abs(), (df.low - pc).abs()))
    return tr.rolling(14, min_periods=5).mean().to_numpy()


def fvg_trades(df5: pd.DataFrame, cost_pct: float = COST_PCT) -> tuple:
    """(gaps, trades) for every session. Pure. gaps: {side, top, bot, born, end, used}; trades: {side, i, entry, stop,
    exit_i, exit, why, pts (after charges)}. Positions are bar indexes into df5."""
    if df5.empty:
        return [], []
    A = atr14(df5)
    O, H, L, C = (df5[k].to_numpy() for k in ("open", "high", "low", "close"))
    tod = (df5.index.hour * 60 + df5.index.minute).to_numpy()
    day = df5.index.normalize()
    grp = pd.Series(np.arange(len(df5)), index=df5.index).groupby(day)
    gaps_all, trades = [], []
    for _, s in grp:
        ix = s.to_numpy()
        if len(ix) < 40 and ix[-1] < len(df5) - 1:          # short (holiday) sessions; never the running day (replay)
            continue
        gaps = []
        for k in ix[2:]:
            a = A[k]
            if not a > 0 or abs(C[k - 1] - O[k - 1]) < 0.8 * a:
                continue
            if L[k] > H[k - 2] and L[k] - H[k - 2] >= 0.1 * a:
                gaps.append({"side": 1, "edge": L[k], "sref": H[k - 2] - 0.1 * a, "top": L[k], "bot": H[k - 2], "born": k,
                             "end": min(k + 24, ix[-1]), "used": None})
            elif H[k] < L[k - 2] and L[k - 2] - H[k] >= 0.1 * a:
                gaps.append({"side": -1, "edge": H[k], "sref": L[k - 2] + 0.1 * a, "top": L[k - 2], "bot": H[k], "born": k,
                             "end": min(k + 24, ix[-1]), "used": None})
        gaps_all += gaps
        live = list(gaps)
        i, done, last = ix[0] + 3, 0, ix[-1]
        while i <= last and done < 2 and tod[i] < EX:
            if not (E0 <= tod[i] <= E1):
                i += 1
                continue
            g = next((x for x in live if x["born"] < i <= x["born"] + 24 and
                      ((x["side"] > 0 and L[i] <= x["edge"]) or (x["side"] < 0 and H[i] >= x["edge"]))), None)
            if g is None:
                i += 1
                continue
            live.remove(g)
            side = g["side"]
            entry = min(O[i], g["edge"]) if side > 0 else max(O[i], g["edge"])
            stop = g["sref"]
            if abs(entry - stop) <= 0:
                i += 1
                continue
            g["used"] = i
            g["end"] = i
            tr, m = {"side": side, "i": i, "entry": float(entry), "stop": float(stop)}, i
            for m in range(i, last + 1):
                if tod[m] >= EX:
                    tr.update(exit_i=m, exit=float(O[m]), why="23:00")
                    break
                if (side > 0 and L[m] <= stop) or (side < 0 and H[m] >= stop):
                    px = stop if m == i else (min(O[m], stop) if side > 0 else max(O[m], stop))
                    tr.update(exit_i=m, exit=float(px), why="stop")
                    break
            if "exit_i" not in tr and last < len(df5) - 1:      # a finished session that ended before 23:00
                tr.update(exit_i=last, exit=float(C[last]), why="day end")
            if "exit_i" in tr:
                tr["pts"] = side * (tr["exit"] - entry) - cost_pct / 100 * entry
            trades.append(tr)
            done += 1
            i = m + 1
    return gaps_all, trades


def tally(trades, df5, slip=0.2) -> str:
    done = [t for t in trades if "pts" in t]
    if not done:
        return "no trades yet"
    p = np.array([t["pts"] for t in done])
    months = max(1, pd.DatetimeIndex([df5.index[t["i"]] for t in done]).to_period("M").nunique())
    return (f"{len(p)} trades · win {round(100 * (p > 0).mean())}% · {p.sum():+.1f} pts · 1 lot ₹{p.sum() * LOT:+,.0f} "
            f"(₹{p.sum() * LOT / months:+,.0f}/month; after {slip} pt slippage ₹{(p - slip).sum() * LOT / months:+,.0f}/month)")


class NGFvg(Indicator):
    id, name = "ng_fvg", "NG FVG (forward test)"
    description = ("MCX natural gas, 5-minute fair value gaps: buy/sell the first pull-back into a fresh gap, stop just beyond "
                   "the gap, hold to 23:00 (no target, no trailing). Positive 2016–25 before slippage, ≈ break-even after — "
                   "paper forward test only.")
    warmup = 300
    params = [Param("show_gaps", "bool", True, "Draw the gap zones"),
              Param("slippage", "choice", "0.2", "Slippage per trade for the ₹ tally (pts)", options=["0", "0.1", "0.2", "0.3"])]

    def compute(self, df, show_gaps, slippage):
        if df.empty or len(df) < 100:
            return Output()
        if df.attrs.get("symbol") not in (None, SYM):
            return Output(info={"note": "Built for MCX NATURALGAS only — FVG lost on NIFTY, SENSEX, crude, gold and silver "
                                        "(tests/fvg_markets.py)."})
        step = np.diff(df.index.values).astype("timedelta64[m]").astype(int)
        if int(np.median(step[step > 0])) != 5:
            return Output(info={"note": "Works on the 5-minute chart only."})
        gaps, trades = fvg_trades(df)
        t = times(df)
        tf = 300
        shapes, markers = [], []
        if show_gaps:
            for g in gaps:
                up = g["side"] > 0
                col = "#26a69a" if up else "#ef5350"
                shapes.append({"kind": "box", "points": [{"time": int(t[g["born"]]), "price": float(g["top"])},
                                                         {"time": int(t[g["end"]]) + tf, "price": float(g["bot"])}],
                               "color": col + "66", "fill": col + "1f", "width": 1, "visible_from": int(t[g["born"]]),
                               "clip": True})
        for tr in trades:
            i, d = tr["i"], tr["side"]
            markers.append({"time": int(t[i]), "position": "belowBar" if d > 0 else "aboveBar",
                            "shape": "arrowUp" if d > 0 else "arrowDown", "color": "#26a69a" if d > 0 else "#ef5350",
                            "visible_from": int(t[i]), "text": f"FVG {'BUY' if d > 0 else 'SELL'} {tr['entry']:.1f}"})
            end = tr.get("exit_i", len(df) - 1)
            shapes.append(seg(t[i], t[end], tr["stop"], "#ef5350", "SL", 1, 1, t[i]))
            if "pts" in tr:
                ok = tr["pts"] > 0
                markers.append({"time": int(t[tr["exit_i"]]), "position": "aboveBar" if d > 0 else "belowBar",
                                "shape": "square", "color": "#26a69a" if ok else "#ef5350",
                                "visible_from": int(t[tr["exit_i"]]),
                                "text": f"{'✓' if ok else '✗'} {tr['why']} {tr['pts']:+.1f} pts (₹{tr['pts'] * LOT:+,.0f})"})
        slip = float(slippage)
        live = [x for x in trades if df.index[x["i"]] >= LIVE_FROM]
        info = {"rules": "FVG (middle candle ≥ 0.8 ATR, gap ≥ 0.1 ATR) → limit at the gap edge on the first pull-back "
                         "(24 bars) · stop beyond the gap · hold to 23:00 · max 2/day · 09:30–22:00",
                "backtest": BACKTEST,
                "on this chart": tally(trades, df, slip),
                "live since 23 Oct 2026": tally(live, df, slip) if live else "no trades yet"}
        today = df.index[-1].normalize()
        tt = [x for x in trades if df.index[x["i"]].normalize() == today]
        if tt:
            x = tt[-1]
            info["today"] = (f"{'BUY' if x['side'] > 0 else 'SELL'} {x['entry']:.1f} stop {x['stop']:.1f} · " +
                             (f"{x['why']} {x['pts']:+.1f} pts" if "pts" in x else "open — hold to 23:00 unless stopped"))
        return Output(shapes=shapes, markers=markers, info=info)


INDICATORS = [NGFvg()]
