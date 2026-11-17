"""S/R reversal (video strategy): price at a prev-day level (PDH / PDL / PDC) + reversal candle + EMA break +
RSI crossing its own moving average, all within a few bars. For manual practice only — the honest 5-min backtest
(tests/ema_rsi_sr.py, NIFTY + BANKNIFTY 2015-Nov 2026) was negative after costs in every variant."""
import numpy as np
import pandas as pd

from ..base import Indicator, Output, Param, ema, line, rma, times
from .levels import seg


def _patterns(o, h, l, c):
    """Vectorised candle patterns. bear[k]/bull[k] = pattern completed at bar k (uses bars k-2..k only)."""
    n = len(c)
    body = np.abs(c - o)
    rng = h - l + 1e-9
    top, bot = np.maximum(o, c), np.minimum(o, c)
    po, pc = np.r_[np.nan, o[:-1]], np.r_[np.nan, c[:-1]]
    p2o, p2c = np.r_[np.nan, np.nan, o[:-2]], np.r_[np.nan, np.nan, c[:-2]]
    pbody = np.abs(pc - po)
    small = body / rng < 0.35
    bear_eng = (c < o) & (pc > po) & (o >= pc) & (c <= po)
    bull_eng = (c > o) & (pc < po) & (o <= pc) & (c >= po)
    star = ((h - top) >= 2 * body) & ((bot - l) <= body) & small        # shooting star
    ham = ((bot - l) >= 2 * body) & ((h - top) <= body) & small          # hammer
    b2 = p2c - p2o
    eve = (b2 > 0) & (pbody <= 0.3 * b2) & (c < o) & (c < p2o + b2 / 2)
    mor = (-b2 > 0) & (pbody <= 0.3 * -b2) & (c > o) & (c > p2c + (-b2) / 2)
    name_bear = np.where(eve, "Evening star", np.where(bear_eng, "Bear engulfing", np.where(star, "Shooting star", "")))
    name_bull = np.where(mor, "Morning star", np.where(bull_eng, "Bull engulfing", np.where(ham, "Hammer", "")))
    name_bear[:2] = ""; name_bull[:2] = ""
    return name_bear, name_bull


def _hm(s):
    try:
        a, b = str(s).split(":")
        return int(a) * 60 + int(b)
    except ValueError:
        return 14 * 60 + 30


class SRReversal(Indicator):
    id, name = "sr_reversal", "S/R Reversal (EMA + RSI + candle)"
    overlay = False                   # RSI gets its own pane; EMA, levels and arrows draw on price (pane 0)
    description = ("Video setup: price at PDH/PDL/PDC + reversal candle + EMA break + RSI crossing its MA. "
                   "Practice only — backtest negative after costs (all 24 variants, 2015–2026).")
    params = [Param("ema_len", "int", 9, "EMA length", 2, 200),
              Param("rsi_len", "int", 14, "RSI length", 2, 50),
              Param("rsi_ma", "int", 14, "RSI MA length", 2, 50),
              Param("window", "int", 3, "All conditions within N bars", 1, 10),
              Param("near_pct", "float", 0.10, "Level touch tolerance %", 0.01, 1.0, 0.01),
              Param("target", "choice", "35 pts", "Target", options=["35 pts", "2R", "none"]),
              Param("max_risk_pct", "float", 0.25, "Skip if stop wider than %", 0.05, 2.0, 0.05),
              Param("skip_min", "int", 15, "No signal first N minutes", 0, 120),
              Param("last_entry", "text", "14:30", "No signal after (HH:MM)"),
              Param("max_per_day", "int", 2, "Max signals per day", 1, 10),
              Param("show_levels", "bool", True, "Show PDH / PDL / PDC"),
              Param("show_patterns", "bool", False, "Mark every candle pattern"),
              Param("show_rsi", "bool", True, "RSI pane")]

    def compute(self, df, ema_len, rsi_len, rsi_ma, window, near_pct, target, max_risk_pct, skip_min, last_entry,
                max_per_day, show_levels, show_patterns, show_rsi):
        t = times(df)
        o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
        n = len(df)
        e = ema(df.close, ema_len).to_numpy()
        d = df.close.diff()
        r = (100 - 100 / (1 + rma(d.clip(lower=0), rsi_len) / rma(-d.clip(upper=0), rsi_len))).to_numpy()
        rm = pd.Series(r).rolling(rsi_ma).mean().to_numpy()
        bear_p, bull_p = _patterns(o, h, l, c)

        day = df.index.normalize()
        tod = df.index.hour * 60 + df.index.minute
        dd = df.groupby(day).agg(H=("high", "max"), L=("low", "min"), C=("close", "last")).shift(1)  # previous day only
        pos = pd.Series(np.arange(n), index=df.index).groupby(day)
        first_i, last_i = pos.first(), pos.last()
        lev = dd.reindex(day).to_numpy()  # per-bar PDH, PDL, PDC

        shapes, mk = [], []
        if show_levels:
            for dy in dd.index:
                if dd.loc[dy].isna().any():
                    continue
                a, b = t[first_i[dy]], t[last_i[dy]]
                for v, col, txt in ((dd.H[dy], "#ff9800", "PDH"), (dd.L[dy], "#ff9800", "PDL"), (dd.C[dy], "#90a4ae", "PDC")):
                    shapes.append(seg(a, b, v, col, txt, 1, 2))
        if show_patterns:
            for k in range(n):
                for nm, up in ((bear_p[k], False), (bull_p[k], True)):
                    if nm:
                        mk.append({"time": int(t[k]), "position": "belowBar" if up else "aboveBar", "shape": "circle",
                                   "color": "rgba(38,166,154,.6)" if up else "rgba(239,83,80,.6)", "text": "",
                                   "visible_from": int(t[k])})

        stop_at = _hm(last_entry)
        count, last_sig = {}, None
        busy = -1
        for i in range(window + 2, n):
            dy = day[i]
            if i <= busy or tod[i] - tod[first_i[dy]] < skip_min or tod[i] > stop_at or count.get(dy, 0) >= max_per_day:
                continue
            if not np.isfinite(lev[i]).all() or not np.isfinite(rm[i]):
                continue
            a = max(i - window, first_i[dy])
            if a >= i:
                continue
            tol = near_pct / 100 * c[i]
            for side in (-1, 1):
                if side < 0:
                    ema_ok = c[i] < e[i] and (c[a:i] >= e[a:i]).any()
                    rsi_ok = r[i] < rm[i] and (r[a:i] >= rm[a:i]).any()
                    pats = [k for k in range(a, i + 1) if bear_p[k]]
                else:
                    ema_ok = c[i] > e[i] and (c[a:i] <= e[a:i]).any()
                    rsi_ok = r[i] > rm[i] and (r[a:i] <= rm[a:i]).any()
                    pats = [k for k in range(a, i + 1) if bull_p[k]]
                if not (ema_ok and rsi_ok and pats):
                    continue
                k0 = max(pats[0] - 1, first_i[dy])
                ext = h[k0:i + 1].max() if side < 0 else l[k0:i + 1].min()
                hit = [nm for v, nm in zip(lev[i], ("PDH", "PDL", "PDC"))
                       if (side < 0 and v - tol <= ext <= v + 2 * tol) or (side > 0 and v - 2 * tol <= ext <= v + tol)]
                if not hit:
                    continue
                stp = ext + side * -1 * 0.0002 * c[i]
                risk = abs(stp - c[i])
                if risk > max_risk_pct / 100 * c[i] or risk <= 0:
                    continue
                pat = (bear_p if side < 0 else bull_p)[pats[-1]]
                up = side > 0
                col = "#26a69a" if up else "#ef5350"
                mk.append({"time": int(t[i]), "position": "belowBar" if up else "aboveBar",
                           "shape": "arrowUp" if up else "arrowDown", "color": col,
                           "text": f"{'BUY' if up else 'SELL'} · {hit[0]} · {pat}", "visible_from": int(t[i])})
                end = t[min(i + 12, last_i[dy])]
                shapes.append(seg(t[i], end, stp, "#ef5350", f"SL {stp:.0f}", 1, 1, t[i]))
                tg = None
                if target != "none":
                    tg = c[i] + side * (35 if target == "35 pts" else 2 * risk)
                    shapes.append(seg(t[i], end, tg, "#26a69a", f"T {tg:.0f}", 1, 1, t[i]))
                count[dy] = count.get(dy, 0) + 1
                busy = i + 3
                last_sig = (df.index[i], "BUY" if up else "SELL", hit[0], pat, c[i], stp, tg, risk)
                break

        series = [line("ema", f"EMA {ema_len}", df, e, "#ffeb3b", pane=0, width=1)]
        if show_rsi:
            series += [line("rsi", f"RSI {rsi_len}", df, r, "#7e57c2", pane=1, width=1),
                       line("rsima", f"RSI MA {rsi_ma}", df, rm, "#ffb74d", pane=1, width=1),
                       line("r70", "70", df, np.full(n, 70.0), "rgba(239,83,80,.4)", pane=1, width=1),
                       line("r30", "30", df, np.full(n, 30.0), "rgba(38,166,154,.4)", pane=1, width=1)]
        info = {"Rules": "price at PDH/PDL/PDC + reversal candle + close through EMA + RSI crosses its MA (within "
                         f"{window} bars). Entry ≈ signal close, SL beyond the pattern extreme.",
                "⚠ Backtest": "5-min NIFTY + BANKNIFTY 2015–Nov 2026: all 24 variants lost after costs "
                              "(≈ −0.07 to −0.34 R/trade). Practise on paper only."}
        if last_sig:
            ts, sd, lv, pat, px, stp, tg, risk = last_sig
            info["Last signal"] = (f"{ts:%d %b %H:%M} {sd} at {lv} ({pat}) · entry ≈ {px:.1f} · SL {stp:.1f} "
                                   f"({risk:.0f} pts)" + (f" · T {tg:.1f}" if tg else ""))
        return Output(series=series, markers=mk, shapes=shapes, info=info)


INDICATORS = [SRReversal()]
