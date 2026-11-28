"""Option BEP levels on the index chart (user's Y-BEP idea, moved from the option chart to the index).

Each day: K = previous index close rounded to the strike step (the ATM strike you would open in the morning),
expiry = the nearest one AFTER today (an expiring contract's premium says little). From the PREVIOUS session's closing
premiums of the K call (CE) and put (PE), real option data (data/hf/options):
    Straddle ±  = K ± (CE + PE)      option sellers' breakevens — the market's priced range
    Half ±      = K ± (CE + PE) / 2
    Call BEP    = K + CE             Put BEP = K − PE   (buyers' breakevens at expiry)
    (optional) CE / PE fractions: K + CE × {0.75, 0.5, 0.33, 0.25}, K − PE × same  (the Y-BEP script's ÷2 family)
Levels are known before the open → no repainting, replay-safe.

TESTED (tests/ybep_index_test.py, NIFTY 2022 → Nov 2026, 15m bounce/break, target 35, after costs): every level lost,
no better than random lines (real − placebo +0.8 pt, t 1.2). The straddle is a good "how far can today go" guide (NIFTY
rarely reaches it) — CONTEXT ONLY, not a buy/sell signal.
"""
import datetime as dt

import numpy as np
import pandas as pd

from ... import options
from ..base import Indicator, Output, Param, times
from .levels import seg

TESTED = "Tested 2022–Nov 2026: no level beat random lines as an entry (bounce −5 pts, break −2 pts/trade) — context only"


def atm(prev_close: float, step: int) -> int:
    return int(round(prev_close / step) * step)


def bep_levels(K: float, ce: float, pe: float, fractions: bool) -> list:
    """[(name, price, colour, width, dash)] — pure."""
    s = ce + pe
    out = [("Straddle +", K + s, "#d32f2f", 2, 0), ("Straddle −", K - s, "#2e7d32", 2, 0),
           ("Half +", K + s / 2, "#ef9a9a", 1, 2), ("Half −", K - s / 2, "#a5d6a7", 1, 2),
           ("Call BEP", K + ce, "#ff9800", 1, 0), ("Put BEP", K - pe, "#7e57c2", 1, 0), ("ATM K", K, "#9e9e9e", 1, 1)]
    if fractions:
        for f, nm in ((0.75, "×1.5"), (0.5, ""), (1 / 3, "/1.5"), (0.25, "/2")):
            out.append((f"CE BEP{nm}", K + ce * f, "#ffcc80", 1, 1))
            out.append((f"PE BEP{nm}", K - pe * f, "#b39ddb", 1, 1))
    return out


def prev_premiums(hf: str, day: pd.Timestamp, prev_day: pd.Timestamp, K: int):
    """(expiry, CE close, PE close) of strike K on prev_day for the nearest expiry after `day`, or None."""
    exps = [e for e in options.dataset_expiries(hf) if e > day.date()]
    if not exps or (exps[0] - day.date()).days > 35:
        return None
    ch = options.load_chain(hf, exps[0])
    if ch is None:
        return None
    x = ch[(ch.strike == K) & (ch.date.dt.normalize() == prev_day) & (ch.date.dt.time <= dt.time(15, 30))]
    ce = x[x.option_type == "CE"].sort_values("date")
    pe = x[x.option_type == "PE"].sort_values("date")
    if ce.empty or pe.empty:
        return None
    return exps[0], float(ce.close.iloc[-1]), float(pe.close.iloc[-1])


class OptionBEP(Indicator):
    id, name = "option_bep", "Option BEP levels (straddle range)"
    description = ("From yesterday's ATM call + put premiums: K ± straddle (option sellers' breakevens), half straddle, "
                   "call / put breakevens (optional Y-BEP fractions). NIFTY / SENSEX intraday. Context only — tested, no "
                   "edge as entries.")
    params = [Param("days", "int", 10, "Draw on the last N days", 1, 60),
              Param("fractions", "bool", False, "Also CE / PE fractions (Y-BEP ×1.5, /2 …)"),
              Param("labels", "bool", True, "Price labels")]

    def compute(self, df, days, fractions, labels):
        sym = df.attrs.get("symbol", "")
        hf = options.root_of(sym) if sym else None
        if df.empty or hf not in ("NIFTY", "SENSEX", "BANKNIFTY"):
            return Output(info={"note": "For NIFTY / SENSEX / BANKNIFTY index charts."})
        step = np.diff(df.index.values).astype("timedelta64[m]").astype(int)
        if len(step) and int(np.median(step[step > 0])) >= 1440:
            return Output(info={"note": "For intraday charts (1-minute to 1-hour)."})
        key = df.index.normalize()
        dlist = list(dict.fromkeys(key))
        closes = df.close.groupby(key).last()
        t = times(df)
        pos = np.arange(len(df))
        shapes, info = [], {"note": TESTED}
        for k in range(max(1, len(dlist) - int(days)), len(dlist)):
            d, p = dlist[k], dlist[k - 1]
            if (d - p).days > 5:
                continue
            K = atm(float(closes[p]), options.STEP.get(hf, 50))
            r = prev_premiums(hf, d, p, K)
            if r is None:
                continue
            exp, ce, pe = r
            ii = pos[key == d]
            a, b = int(ii[0]), int(ii[-1])
            for name, price, col, w, dash in bep_levels(K, ce, pe, bool(fractions)):
                shapes.append(seg(t[a], t[b], price, col, f"{name} {price:,.0f}" if labels else "", w, dash, int(t[a])))
            info["latest"] = (f"{d:%d %b}: ATM {K} ({exp:%d %b} expiry) · CE {ce:.1f} + PE {pe:.1f} = straddle {ce + pe:.1f} "
                              f"→ range {K - ce - pe:,.0f} – {K + ce + pe:,.0f} · Call BEP {K + ce:,.0f} · Put BEP {K - pe:,.0f}")
        if "latest" not in info:
            info["latest"] = "no option data for these days"
        return Output(shapes=shapes, info=info)


INDICATORS = [OptionBEP()]
