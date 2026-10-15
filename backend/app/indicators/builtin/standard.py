"""Standard indicators: moving averages, Bollinger, RSI, MACD, Supertrend, VWAP, EMA crossover."""
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


class Bollinger(Indicator):
    id, name, description = "bb", "Bollinger Bands", "SMA ± k standard deviations."
    params = [Param("length", "int", 20, "Length", 2, 500), Param("mult", "float", 2.0, "StdDev", 0.1, 10, 0.1),
              Param("color", "color", "#26a69a", "Color")]

    def compute(self, df, length, mult, color):
        m = df.close.rolling(length).mean()
        s = df.close.rolling(length).std(ddof=0)
        return Output(series=[line("basis", "Basis", df, m, "#ff9800", width=1),
                              line("upper", "Upper", df, m + mult * s, color, width=1),
                              line("lower", "Lower", df, m - mult * s, color, width=1)])


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


INDICATORS = [SMA(), EMA(), Bollinger(), RSI(), MACD(), Supertrend(), VWAP(), EMACross()]
