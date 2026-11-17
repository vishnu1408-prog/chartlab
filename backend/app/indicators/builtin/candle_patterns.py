"""Candlestick patterns with an on/off switch for every pattern.

REPLAY CONTRACT: a pattern ending at bar i uses bars i-2..i only (plus the trend / average-body / ATR values up to the
bar before the pattern), so it is known at the close of bar i. With "Confirmation = next candle" it appears only at the
close of bar i+1 (visible_from = time of bar i+1).

Definitions (body = |close-open|, range = high-low, avg body = EMA(body, 14), shadows = wicks):
  Doji            body <= Doji % of range            Dragonfly / Gravestone: doji with (almost) no upper / lower wick
  Spinning top    small body (< avg) and both wicks longer than the body
  Hammer          lower wick >= 2 x body, tiny upper wick, small body, after a DOWN trend  (same shape after an
  Hanging man     UP trend = hanging man).  Inverted hammer / Shooting star = mirror (long upper wick)
  Marubozu        long body (> avg) with wicks <= Marubozu % of range on both sides
  Engulfing       body opposite colour to the previous candle and covers its whole body (and is bigger)
  Harami          previous candle long (> avg body); current body opposite colour, inside the previous body and
                  <= 60 % of it.  Harami cross = the inside candle is a doji
  Piercing / Dark cloud   after a long opposite candle, opens beyond its close and closes past the middle of its body
  Tweezer bottom / top    two opposite-colour candles with equal lows / highs (within Tweezer tolerance x ATR)
  Morning / Evening star  long candle, small middle candle, third candle closes past the middle of the first body
  Three white soldiers / black crows  three long same-colour candles, each closing further, small wicks
  Three inside up / down  harami followed by a candle that closes beyond the first candle's open
Trend = close of the bar before the pattern vs its SMA(Trend length). Reversal patterns need the opposite trend when
"Reversals need a prior trend" is on (hammer/hanging man/shooting star/inverted hammer always use it - that is what
separates them).
"""
import numpy as np
import pandas as pd

from ..base import Indicator, Output, Param, ema, times, true_range

# key, label, short code, side (+1 bull, -1 bear, 0 neutral), bars, default on, group
PATTERNS = [
    ("doji", "Doji", "D", 0, 1, True, "Single candle"),
    ("dragonfly", "Dragonfly doji", "DD", 1, 1, False, "Single candle"),
    ("gravestone", "Gravestone doji", "GD", -1, 1, False, "Single candle"),
    ("spinning", "Spinning top", "ST", 0, 1, False, "Single candle"),
    ("hammer", "Hammer", "H", 1, 1, True, "Single candle"),
    ("inv_hammer", "Inverted hammer", "IH", 1, 1, False, "Single candle"),
    ("hanging", "Hanging man", "HM", -1, 1, False, "Single candle"),
    ("shooting", "Shooting star", "SS", -1, 1, True, "Single candle"),
    ("bull_maru", "Bullish marubozu", "BM", 1, 1, False, "Single candle"),
    ("bear_maru", "Bearish marubozu", "SM", -1, 1, False, "Single candle"),
    ("bull_eng", "Bullish engulfing", "BE", 1, 2, True, "Two candles"),
    ("bear_eng", "Bearish engulfing", "SE", -1, 2, True, "Two candles"),
    ("bull_harami", "Bullish harami", "BH", 1, 2, True, "Two candles"),
    ("bear_harami", "Bearish harami", "SH", -1, 2, True, "Two candles"),
    ("bull_hcross", "Bullish harami cross", "BH+", 1, 2, False, "Two candles"),
    ("bear_hcross", "Bearish harami cross", "SH+", -1, 2, False, "Two candles"),
    ("piercing", "Piercing line", "PL", 1, 2, False, "Two candles"),
    ("dark_cloud", "Dark cloud cover", "DC", -1, 2, False, "Two candles"),
    ("tw_bottom", "Tweezer bottom", "TB", 1, 2, False, "Two candles"),
    ("tw_top", "Tweezer top", "TT", -1, 2, False, "Two candles"),
    ("morning", "Morning star", "MS", 1, 3, False, "Three candles"),
    ("evening", "Evening star", "ES", -1, 3, False, "Three candles"),
    ("soldiers", "Three white soldiers", "3W", 1, 3, False, "Three candles"),
    ("crows", "Three black crows", "3B", -1, 3, False, "Three candles"),
    ("in_up", "Three inside up", "3IU", 1, 3, False, "Three candles"),
    ("in_down", "Three inside down", "3ID", -1, 3, False, "Three candles"),
]
META = {p[0]: p for p in PATTERNS}


def sh(a, k):
    """value k bars back (NaN / False before the start)"""
    a = np.asarray(a)
    if a.dtype == bool:
        return np.r_[np.zeros(k, bool), a[:-k]] if k else a
    return np.r_[np.full(k, np.nan), a[:-k].astype(float)] if k else a.astype(float)


def detect(df, doji_pct=5.0, maru_pct=5.0, trend_len=10, need_trend=True, min_atr=0.1, tw_tol=0.05, same_day=True):
    """Return {key: bool array}; True at the LAST bar of the pattern. Uses bars <= i only."""
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    n = len(c)
    body, rng = np.abs(c - o), h - l
    bhi, blo = np.maximum(o, c), np.minimum(o, c)
    ush, lsh = h - bhi, blo - l
    up, dn = c > o, c < o
    avgb = sh(ema(pd.Series(body), 14).to_numpy(), 1)             # average body of the bars BEFORE this one
    atr = sh(ema(true_range(df), 14).to_numpy(), 1)
    with np.errstate(invalid="ignore"):
        valid = (rng > 0) & ~(rng < min_atr * atr)                 # ignore tiny / flat candles
        long_ = body > avgb
        small = body < avgb
        doji = valid & (body <= doji_pct / 100 * rng)
        sma = pd.Series(c).rolling(trend_len, min_periods=trend_len).mean().to_numpy()
        upT_now, dnT_now = c > sma, c < sma

    def upT(k):                                                    # trend at the bar before a k-bar pattern
        return sh(upT_now, k)

    def dnT(k):
        return sh(dnT_now, k)

    tr = (lambda a: a) if need_trend else (lambda a: np.ones(n, bool))
    if same_day and n > 1 and np.median(np.diff(df.index.values).astype("timedelta64[s]").astype(float)) < 86400:
        d = df.index.normalize().values
        same1 = np.r_[False, d[1:] == d[:-1]]
    else:
        same1 = np.r_[False, np.ones(n - 1, bool)]
    same2 = same1 & sh(same1, 1)

    o1, h1, l1, c1 = sh(o, 1), sh(h, 1), sh(l, 1), sh(c, 1)
    o2, c2 = sh(o, 2), sh(c, 2)
    body1, body2 = sh(body, 1), sh(body, 2)
    bhi1, blo1 = sh(bhi, 1), sh(blo, 1)
    up1, dn1, up2, dn2 = sh(up, 1), sh(dn, 1), sh(up, 2), sh(dn, 2)
    long1, long2, small1 = sh(long_, 1), sh(long_, 2), sh(small, 1)
    doji1, valid1 = sh(doji, 1), sh(valid, 1)
    R = {}
    with np.errstate(invalid="ignore"):
        tiny_u, tiny_l = ush <= 0.1 * rng, lsh <= 0.1 * rng
        R["dragonfly"] = doji & tiny_u & (lsh >= 0.6 * rng)
        R["gravestone"] = doji & tiny_l & (ush >= 0.6 * rng)
        R["doji"] = doji
        R["spinning"] = valid & ~doji & small & (body <= 0.3 * rng) & (ush >= body) & (lsh >= body)
        ham = valid & ~doji & (lsh >= 2 * body) & tiny_u & (body <= 0.35 * rng)
        inv = valid & ~doji & (ush >= 2 * body) & tiny_l & (body <= 0.35 * rng)
        R["hammer"], R["hanging"] = ham & dnT(1), ham & upT(1)
        R["inv_hammer"], R["shooting"] = inv & dnT(1), inv & upT(1)
        maru = valid & long_ & (ush <= maru_pct / 100 * rng) & (lsh <= maru_pct / 100 * rng)
        R["bull_maru"], R["bear_maru"] = maru & up, maru & dn

        base2 = same1 & valid & valid1
        R["bull_eng"] = base2 & up & dn1 & (bhi >= bhi1) & (blo <= blo1) & (body > body1) & ~doji1 & tr(dnT(2))
        R["bear_eng"] = base2 & dn & up1 & (bhi >= bhi1) & (blo <= blo1) & (body > body1) & ~doji1 & tr(upT(2))
        inside = (bhi <= bhi1) & (blo >= blo1)
        R["bull_harami"] = base2 & dn1 & long1 & up & inside & (body <= 0.6 * body1) & ~doji & tr(dnT(2))
        R["bear_harami"] = base2 & up1 & long1 & dn & inside & (body <= 0.6 * body1) & ~doji & tr(upT(2))
        R["bull_hcross"] = base2 & dn1 & long1 & doji & inside & tr(dnT(2))
        R["bear_hcross"] = base2 & up1 & long1 & doji & inside & tr(upT(2))
        mid1 = (o1 + c1) / 2
        R["piercing"] = base2 & dn1 & long1 & up & (o <= c1) & (c > mid1) & (c < o1) & tr(dnT(2))
        R["dark_cloud"] = base2 & up1 & long1 & dn & (o >= c1) & (c < mid1) & (c > o1) & tr(upT(2))
        tol = tw_tol * atr
        R["tw_bottom"] = base2 & dn1 & up & (np.abs(l - l1) <= tol) & ~doji & ~doji1 & tr(dnT(2))
        R["tw_top"] = base2 & up1 & dn & (np.abs(h - h1) <= tol) & ~doji & ~doji1 & tr(upT(2))

        base3 = same2 & valid & valid1 & sh(valid, 2)
        mid2 = (o2 + c2) / 2
        R["morning"] = base3 & dn2 & long2 & small1 & (blo1 < c2) & up & (c > mid2) & tr(dnT(3))
        R["evening"] = base3 & up2 & long2 & small1 & (bhi1 > c2) & dn & (c < mid2) & tr(upT(3))
        eps, eps1 = 0.05 * atr, sh(0.05 * atr, 1)        # intraday candles open at ~ the previous close
        wick_ok = (ush <= 0.3 * body)
        R["soldiers"] = (base3 & up & up1 & up2 & long_ & long1 & long2 & (c > c1) & (c1 > c2)
                         & (o >= blo1 - eps) & (o <= bhi1 + eps) & (o1 >= sh(blo, 2) - eps1) & (o1 <= sh(bhi, 2) + eps1)
                         & wick_ok & sh(wick_ok, 1) & sh(wick_ok, 2))
        wick_ok2 = (lsh <= 0.3 * body)
        R["crows"] = (base3 & dn & dn1 & dn2 & long_ & long1 & long2 & (c < c1) & (c1 < c2)
                      & (o >= blo1 - eps) & (o <= bhi1 + eps) & (o1 >= sh(blo, 2) - eps1) & (o1 <= sh(bhi, 2) + eps1)
                      & wick_ok2 & sh(wick_ok2, 1) & sh(wick_ok2, 2))
        R["in_up"] = base3 & sh(R["bull_harami"], 1) & up & (c > o2)
        R["in_down"] = base3 & sh(R["bear_harami"], 1) & dn & (c < o2)
    for k in R:
        R[k] = np.nan_to_num(R[k].astype(float)).astype(bool)
    return R


# more specific name wins when two patterns describe the same bar
SUPERSEDES = {"dragonfly": ["doji", "hammer"], "gravestone": ["doji", "shooting", "inv_hammer"],
              "bull_hcross": ["doji"], "bear_hcross": ["doji"], "in_up": ["bull_eng", "bull_maru"],
              "in_down": ["bear_eng", "bear_maru"], "morning": ["bull_eng", "piercing"],
              "evening": ["bear_eng", "dark_cloud"], "soldiers": ["bull_maru"], "crows": ["bear_maru"]}


class CandlePatterns(Indicator):
    id, name = "candle_patterns", "Candle Patterns"
    overlay = True
    warmup = 60
    description = ("Marks doji, harami, engulfing, hammer, stars and 20+ other candlestick patterns. Every pattern has "
                   "its own on/off switch. A visual aid: tested alone on NIFTY 5m/15m (2015–2026) none beat chance.")
    params = ([Param(k, "bool", on, lab, group=g) for k, lab, _, _, _, on, g in PATTERNS] + [
        Param("confirm", "choice", "None", "Confirmation", options=["None", "Next candle"], group="Settings"),
        Param("need_trend", "bool", True, "Reversals need a prior trend", group="Settings"),
        Param("trend_len", "int", 10, "Trend SMA length", 2, 200, group="Settings"),
        Param("doji_pct", "float", 5.0, "Doji: body ≤ % of range", 1, 30, 0.5, group="Settings"),
        Param("maru_pct", "float", 5.0, "Marubozu: wicks ≤ % of range", 0, 20, 0.5, group="Settings"),
        Param("tw_tol", "float", 0.05, "Tweezer: equal within × ATR", 0.01, 0.5, 0.01, group="Settings"),
        Param("min_atr", "float", 0.1, "Ignore candles smaller than × ATR", 0, 1, 0.05, group="Settings"),
        Param("same_day", "bool", True, "Intraday: don't join candles across days", group="Settings"),
        Param("labels", "choice", "Name", "Label", options=["Name", "Short", "None"], group="Style"),
        Param("bull_color", "color", "#26a69a", "Bullish colour", group="Style"),
        Param("bear_color", "color", "#ef5350", "Bearish colour", group="Style"),
        Param("neutral_color", "color", "#b0bec5", "Neutral colour (doji, spinning top)", group="Style")])

    def compute(self, df, **p):
        on = [k for k, *_ in PATTERNS if p.get(k)]
        n = len(df)
        if not on or n < 3:
            return Output(info={"Patterns": "none switched on — open ⚙ settings" if not on else "not enough candles"})
        R = detect(df, p["doji_pct"], p["maru_pct"], p["trend_len"], p["need_trend"], p["min_atr"], p["tw_tol"],
                   p["same_day"])
        t = times(df)
        c = df.close.to_numpy(float)
        conf = p["confirm"] == "Next candle"
        if conf:   # bull: next close above this close, bear: below (neutral patterns are not filtered)
            nxt = np.r_[c[1:], np.nan]
            with np.errstate(invalid="ignore"):
                ok_bull, ok_bear = nxt > c, nxt < c
        hits = {k: R[k].copy() for k in on}
        for big, smalls in SUPERSEDES.items():
            if big in hits:
                for s in smalls:
                    if s in hits:
                        hits[s] &= ~hits[big]
        col = {1: p["bull_color"], -1: p["bear_color"], 0: p["neutral_color"]}
        mk, last = [], {}
        idx = np.flatnonzero(np.any(np.vstack([hits[k] for k in on]), axis=0))
        for i in idx:
            groups = {1: [], -1: [], 0: []}
            for k in on:
                if not hits[k][i]:
                    continue
                side = META[k][3]
                if conf and side and not (ok_bull[i] if side > 0 else ok_bear[i]):
                    continue
                groups[side].append(k)
            for side, ks in groups.items():
                if not ks:
                    continue
                vis = int(t[i + 1]) if (conf and side and i + 1 < n) else int(t[i])
                txt = "" if p["labels"] == "None" else " · ".join(META[k][1 if p["labels"] == "Name" else 2] for k in ks)
                mk.append({"time": int(t[i]), "position": "belowBar" if side > 0 else "aboveBar",
                           "shape": "arrowUp" if side > 0 else "arrowDown" if side < 0 else "circle",
                           "color": col[side], "text": txt, "visible_from": vis})
                for k in ks:
                    last[k] = i
        mk.sort(key=lambda m: m["time"])
        info = {"On": ", ".join(META[k][1] for k in on),
                "Note": "Arrows = bullish/bearish pattern, dot = indecision (doji / spinning top). "
                        + ("Shown after the next candle confirms. " if conf else "Shown at the pattern candle's close. ")
                        + "Visual aid only: alone, candle patterns did not beat chance on NIFTY 5m/15m (2015–2026)."}
        return Output(markers=mk, info=info)


INDICATORS = [CandlePatterns()]
