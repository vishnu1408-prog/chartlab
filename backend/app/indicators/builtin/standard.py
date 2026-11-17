"""Standard indicators: moving averages (incl. EMA 21/50/200 ribbon), Bollinger, RSI, MACD, Supertrend, VWAP, EMA crossover."""
import numpy as np
import pandas as pd

from ..base import Indicator, Output, Param, ema, histogram, line, rma, times, true_range

SRC = Param("source", "choice", "close", "Source", options=["close", "open", "high", "low", "hl2", "hlc3"])


def source(df, name):
    if name == "hl2":
        return (df.high + df.low) / 2
    if name == "hlc3":
        return (df.high + df.low + df.close) / 3
    return df[name]


class SMA(Indicator):
    id, name, description = "sma", "Moving Average (SMA)", "Simple moving average."
    params = [Param("length", "int", 20, "Length", 1, 500), SRC, Param("color", "color", "#f5a623", "Color")]

    def compute(self, df, length, source, color):
        v = globals()["source"](df, source).rolling(length).mean()
        return Output(series=[line("sma", f"SMA {length}", df, v, color)])


class EMA(Indicator):
    id, name, description = "ema", "Moving Average (EMA)", "Exponential moving average."
    params = [Param("length", "int", 21, "Length", 1, 500), SRC, Param("color", "color", "#2962ff", "Color")]

    def compute(self, df, length, source, color):
        return Output(series=[line("ema", f"EMA {length}", df, ema(globals()["source"](df, source), length), color)])


class EMARibbon(Indicator):
    id, name = "ema_ribbon", "EMA 21 / 50 / 200"
    description = "Three EMAs in one (default 21, 50, 200) with the trend stack in the legend."
    warmup = 1200                                          # the 200 EMA needs a long run-in to settle
    params = [Param("show_fast", "bool", True, "Show fast EMA"), Param("fast", "int", 21, "Fast", 1, 500),
              Param("show_mid", "bool", True, "Show middle EMA"), Param("mid", "int", 50, "Middle", 1, 500),
              Param("show_slow", "bool", True, "Show slow EMA"), Param("slow", "int", 200, "Slow", 1, 1000), SRC,
              Param("c_fast", "color", "#2962ff", "Fast colour"), Param("c_mid", "color", "#f5a623", "Middle colour"),
              Param("c_slow", "color", "#e040fb", "Slow colour")]

    def compute(self, df, fast, mid, slow, source, c_fast, c_mid, c_slow, show_fast=True, show_mid=True, show_slow=True):
        src = globals()["source"](df, source)
        e = {n: ema(src, n) for n in (fast, mid, slow)}
        series = [line(f"ema{n}", f"EMA {n}", df, e[n], c, width=w)
                  for n, c, w, on in ((fast, c_fast, 1, show_fast), (mid, c_mid, 2, show_mid), (slow, c_slow, 2, show_slow))
                  if on]
        info = {}
        if len(df) > slow:
            p, f, m, s_ = (float(x.iloc[-1]) for x in (df.close, e[fast], e[mid], e[slow]))
            stack = ("bullish stack (fast > middle > slow)" if f > m > s_ else
                     "bearish stack (fast < middle < slow)" if f < m < s_ else "mixed — no clean trend")
            shown = [(n, v) for n, v, on in ((fast, f, show_fast), (mid, m, show_mid), (slow, s_, show_slow)) if on]
            above = sum(p > v for _, v in shown)
            info = {"trend": stack,
                    "price": (f"above {above} of {len(shown)} shown EMAs" if shown else "no EMA shown"),
                    "values": " · ".join(f"EMA {n} {v:,.2f}" for n, v in shown)}
        return Output(series=series, info=info)


class Bollinger(Indicator):
    id, name = "bb", "Bollinger Bands"
    description = ("Basis (SMA/EMA) ± k standard deviations, optional 1σ/3σ bands, squeeze dots (narrowest bands in "
                   "N bars), close-outside markers; legend shows %B and band width. Same maths as TradingView (σ ddof=0).")
    params = [Param("length", "int", 20, "Length", 2, 500, group="Bands"),
              Param("mult", "float", 2.0, "StdDev", 0.1, 10, 0.1, group="Bands"),
              Param("source", "choice", "close", "Source", options=["close", "hlc3", "hl2", "ohlc4"], group="Bands"),
              Param("ma_type", "choice", "SMA", "Basis type", options=["SMA", "EMA"], group="Bands"),
              Param("extra", "choice", "None", "Extra bands", options=["None", "1σ", "3σ", "1σ + 3σ"], group="Bands"),
              Param("squeeze", "bool", True, "Squeeze dots", group="Signals"),
              Param("sq_len", "int", 120, "Squeeze = narrowest width in N bars", 20, 1000, group="Signals"),
              Param("outside", "bool", False, "Mark closes outside the bands", group="Signals"),
              Param("color", "color", "#26a69a", "Band colour", group="Display"),
              Param("basis_color", "color", "#ff9800", "Basis colour", group="Display"),
              Param("show_basis", "bool", True, "Show basis", group="Display")]

    def compute(self, df, length, mult, source, ma_type, extra, squeeze, sq_len, outside, color, basis_color,
                show_basis):
        src = {"close": df.close, "hlc3": (df.high + df.low + df.close) / 3, "hl2": (df.high + df.low) / 2,
               "ohlc4": (df.open + df.high + df.low + df.close) / 4}[source]
        m = src.rolling(length).mean() if ma_type == "SMA" else ema(src, length)
        sd = src.rolling(length).std(ddof=0)
        up, lo = m + mult * sd, m - mult * sd
        series = []
        if show_basis:
            series.append(line("basis", "Basis", df, m, basis_color, width=1))
        series += [line("upper", f"Upper {mult:g}σ", df, up, color, width=1),
                   line("lower", f"Lower {mult:g}σ", df, lo, color, width=1)]
        for k in ({"1σ": [1.0], "3σ": [3.0], "1σ + 3σ": [1.0, 3.0]}.get(extra, [])):
            for sgn, nm in ((1, "up"), (-1, "dn")):
                s_ = line(f"{nm}{k:g}", f"{'+' if sgn > 0 else '−'}{k:g}σ", df, m + sgn * k * sd, color, width=1)
                s_["lineStyle"] = 2
                series.append(s_)
        t = times(df)
        c = df.close.to_numpy(float)
        width = ((up - lo) / m * 100).to_numpy(float)                  # band width, % of price
        with np.errstate(invalid="ignore", divide="ignore"):
            pb = ((df.close - lo) / (up - lo)).to_numpy(float)        # %B: 0 = lower band, 1 = upper band
        is_sq = width <= pd.Series(width).rolling(sq_len, min_periods=sq_len).min().to_numpy() + 1e-12
        mk = []
        if squeeze:
            for i in np.where(is_sq & np.isfinite(width))[0]:
                mk.append({"time": int(t[i]), "position": "belowBar", "shape": "circle", "color": "#ffca28",
                           "text": "SQZ" if i == 0 or not is_sq[i - 1] else "", "visible_from": int(t[i])})
        if outside:
            u, l_ = up.to_numpy(float), lo.to_numpy(float)
            for i in range(1, len(c)):
                if c[i] > u[i] and not c[i - 1] > u[i - 1]:
                    mk.append({"time": int(t[i]), "position": "aboveBar", "shape": "circle", "color": "#ef5350",
                               "text": "↑BB", "visible_from": int(t[i])})
                elif c[i] < l_[i] and not c[i - 1] < l_[i - 1]:
                    mk.append({"time": int(t[i]), "position": "belowBar", "shape": "circle", "color": "#26a69a",
                               "text": "↓BB", "visible_from": int(t[i])})
        info = {}
        if len(c) and np.isfinite(up.iloc[-1]):
            info["Now"] = (f"U {up.iloc[-1]:,.1f} · B {m.iloc[-1]:,.1f} · L {lo.iloc[-1]:,.1f} · "
                           f"width {up.iloc[-1] - lo.iloc[-1]:.0f} pts ({width[-1]:.2f} %) · %B {pb[-1]:.2f}"
                           + (" · SQUEEZE" if is_sq[-1] else ""))
        info["How to read"] = ("%B > 1 = closed above upper band, < 0 = below lower. Squeeze = narrowest bands in "
                               f"{sq_len} bars → a big move often follows, direction unknown. Riding a band = trend; "
                               "tags inside a range = stretched.")
        return Output(series=series, markers=mk, info=info)


class RSI(Indicator):
    id, name, description, overlay = "rsi", "RSI", "Relative Strength Index (Wilder).", False
    params = [Param("length", "int", 14, "Length", 2, 200), Param("upper", "float", 70, "Overbought"),
              Param("lower", "float", 30, "Oversold")]

    def compute(self, df, length, upper, lower):
        ch = df.close.diff()
        up, dn = rma(ch.clip(lower=0), length), rma((-ch).clip(lower=0), length)
        r = 100 - 100 / (1 + up / dn.replace(0, np.nan))
        r = r.where(dn != 0, 100.0)
        r.iloc[:length] = np.nan
        n = len(df)
        return Output(series=[line("rsi", f"RSI {length}", df, r, "#7e57c2", pane=1),
                              line("ob", "OB", df, np.full(n, upper), "rgba(239,83,80,0.5)", pane=1, width=1),
                              line("os", "OS", df, np.full(n, lower), "rgba(38,166,154,0.5)", pane=1, width=1)])


class MACD(Indicator):
    id, name, description, overlay = "macd", "MACD", "EMA(fast) − EMA(slow), signal line and histogram.", False
    params = [Param("fast", "int", 12, "Fast", 1, 200), Param("slow", "int", 26, "Slow", 2, 400),
              Param("signal", "int", 9, "Signal", 1, 100)]

    def compute(self, df, fast, slow, signal):
        m = ema(df.close, fast) - ema(df.close, slow)
        s = ema(m, signal)
        h = (m - s).to_numpy()
        cols = ["#26a69a" if (x >= 0) else "#ef5350" for x in np.nan_to_num(h)]
        return Output(series=[histogram("hist", "Histogram", df, h, cols, pane=1),
                              line("macd", "MACD", df, m, "#2962ff", pane=1, width=1),
                              line("signal", "Signal", df, s, "#ff6d00", pane=1, width=1)])


def supertrend(df, length, mult):
    atr = rma(true_range(df), length).to_numpy()
    h, l, c = df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
    hl2 = (h + l) / 2
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    fu, fl = ub.copy(), lb.copy()
    tr = np.ones(len(c))
    for i in range(1, len(c)):
        fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        tr[i] = 1 if c[i] > fu[i - 1] else (-1 if c[i] < fl[i - 1] else tr[i - 1])
    return tr, fu, fl


class Supertrend(Indicator):
    id, name, description = "supertrend", "Supertrend", "ATR trailing trend line (green = up, red = down)."
    params = [Param("length", "int", 10, "ATR length", 1, 100), Param("mult", "float", 3.0, "Multiplier", 0.5, 10, 0.1)]

    def compute(self, df, length, mult):
        tr, fu, fl = supertrend(df, length, mult)
        up = np.where(tr == 1, fl, np.nan)
        dn = np.where(tr == -1, fu, np.nan)
        up[:length], dn[:length] = np.nan, np.nan
        t = times(df)
        flips = np.flatnonzero(np.diff(tr) != 0) + 1
        mk = [{"time": int(t[i]), "position": "belowBar" if tr[i] == 1 else "aboveBar",
               "shape": "arrowUp" if tr[i] == 1 else "arrowDown", "color": "#26a69a" if tr[i] == 1 else "#ef5350",
               "text": "", "visible_from": int(t[i])} for i in flips if i >= length]
        return Output(series=[line("up", "Up", df, up, "#26a69a"), line("down", "Down", df, dn, "#ef5350")], markers=mk)


class VWAP(Indicator):
    id, name, description = "vwap", "VWAP (session)", "Volume-weighted average price, resets daily. Needs volume (stocks)."
    params = [Param("color", "color", "#e040fb", "Color")]

    def compute(self, df, color):
        if df.volume.sum() <= 0:
            return Output(info={"note": "No volume in this data (NSE indices publish none) — use a stock or futures."})
        tp = (df.high + df.low + df.close) / 3
        day = df.index.normalize()
        pv = (tp * df.volume).groupby(day).cumsum()
        vv = df.volume.groupby(day).cumsum()
        return Output(series=[line("vwap", "VWAP", df, (pv / vv.replace(0, np.nan)).to_numpy(), color)])


class EMACross(Indicator):
    id, name, description = "ema_cross", "EMA Crossover", "Fast/slow EMA lines with BUY/SELL markers at crosses."
    params = [Param("fast", "int", 12, "Fast", 1, 300), Param("slow", "int", 26, "Slow", 2, 500),
              Param("intraday_only", "bool", True, "Signals 09:30–14:30 only")]

    def compute(self, df, fast, slow, intraday_only):
        f, s = ema(df.close, fast), ema(df.close, slow)
        x = np.sign(f - s).diff().fillna(0).to_numpy() / 2
        t = times(df)
        tod = df.index.hour * 60 + df.index.minute
        mk = []
        for i in np.flatnonzero(x != 0):
            if i < slow or (intraday_only and not (570 <= tod[i] <= 870)):
                continue
            up = x[i] > 0
            mk.append({"time": int(t[i]), "position": "belowBar" if up else "aboveBar", "shape": "arrowUp" if up else "arrowDown",
                       "color": "#26a69a" if up else "#ef5350", "text": "BUY" if up else "SELL", "visible_from": int(t[i])})
        return Output(series=[line("fast", f"EMA {fast}", df, f, "#2962ff", width=1),
                              line("slow", f"EMA {slow}", df, s, "#ff6d00", width=1)], markers=mk)


INDICATORS = [SMA(), EMA(), EMARibbon(), Bollinger(), RSI(), MACD(), Supertrend(), VWAP(), EMACross()]
