"""Mean Reversion: a mean line with ±σ bands, a z-score pane and "snap-back" fade signals.

Idea: when price stretches far from its mean (beyond the inner band) and then a candle closes back inside the band
against the stretch, fade it toward the mean. Stop beyond the stretch extreme, target = the mean line (or 35 pts / 1.5R).

Replay-safe: every value at bar i uses bars <= i; signals use only closed bars and carry visible_from = signal bar.
`find_signals()` is shared with the research script (~/indicators/tests/mr_test.py) so the backtest tests exactly this code.
"""
import numpy as np
import pandas as pd

from ..base import Indicator, Output, Param, ema, histogram, line, rma, times, true_range
from .levels import seg

CENTERS = ["EMA", "SMA", "Session mean", "VWAP"]
TARGETS = ["Mean line", "35 pts", "1.5R"]


def _hm(s, dflt=14 * 60 + 30):
    try:
        a, b = str(s).split(":")
        return int(a) * 60 + int(b)
    except ValueError:
        return dflt


def bands(df, center="EMA", length=20):
    """Return (mean, sigma) arrays. sigma = rolling std of (close - mean) over `length` bars."""
    c = df.close
    day = df.index.normalize()
    if center == "VWAP" and df.volume.sum() > 0:
        tp = (df.high + df.low + df.close) / 3
        m = (tp * df.volume).groupby(day).cumsum() / df.volume.groupby(day).cumsum().replace(0, np.nan)
    elif center in ("Session mean", "VWAP"):        # VWAP without volume (indices) → equal-weight session average
        tp = (df.high + df.low + df.close) / 3
        m = tp.groupby(day).cumsum() / tp.groupby(day).cumcount().add(1)
    elif center == "SMA":
        m = c.rolling(length).mean()
    else:
        m = ema(c, length)
    sig = (c - m).rolling(length).std(ddof=0)
    return m.to_numpy(float), sig.to_numpy(float)


def find_signals(df, center="EMA", length=20, k_in=2.0, k_out=3.0, lookback=3, need_candle=True, rsi_filter=False,
            rsi_len=14, rsi_hi=70.0, rsi_lo=30.0, skip_runaway=False, target="Mean line", min_rr=1.0,
            max_risk_pct=0.25, skip_min=15, last_entry="14:30", max_per_day=2, stop_buf_atr=0.1):
    """Fade signals. Returns (list of dicts, mean, sigma, z)."""
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    n = len(df)
    m, s = bands(df, center, length)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (c - m) / s
        zh, zl = (h - m) / s, (l - m) / s
    d = df.close.diff()
    rsi = (100 - 100 / (1 + rma(d.clip(lower=0), rsi_len) / rma(-d.clip(upper=0), rsi_len))).to_numpy()
    atr = rma(true_range(df), 14).to_numpy()
    day = df.index.normalize()
    dcode = pd.factorize(day)[0]
    tod = (df.index.hour * 60 + df.index.minute).to_numpy()
    first_t = pd.Series(tod).groupby(dcode).transform("first").to_numpy()
    stop_at = _hm(last_entry)

    out, count, used = [], {}, {}
    for i in range(max(length, rsi_len) + 2, n):
        if not (np.isfinite(s[i]) and s[i] > 0 and np.isfinite(m[i])):
            continue
        dy = dcode[i]
        if tod[i] - first_t[i] < skip_min or tod[i] > stop_at or count.get(dy, 0) >= max_per_day:
            continue
        a = i - lookback
        while a < i and dcode[a] != dy:            # stretch must be on the same day
            a += 1
        if a >= i:
            continue
        for side in (-1, 1):                       # -1 = SELL (fade an up-stretch), +1 = BUY (fade a down-stretch)
            if side < 0:
                hit = np.where(zh[a:i] >= k_in)[0]
                back = z[i] < k_in and c[i] < c[i - 1] and (not need_candle or c[i] < o[i])
            else:
                hit = np.where(zl[a:i] <= -k_in)[0]
                back = z[i] > -k_in and c[i] > c[i - 1] and (not need_candle or c[i] > o[i])
            if not len(hit) or not back:
                continue
            k0 = a + hit[0]
            if used.get(side, -1) >= k0:           # one signal per stretch
                continue
            seg_h, seg_l = h[k0:i + 1].max(), l[k0:i + 1].min()
            if rsi_filter:
                rr_ = rsi[k0:i]
                if (side < 0 and not np.nanmax(rr_) >= rsi_hi) or (side > 0 and not np.nanmin(rr_) <= rsi_lo):
                    continue
            if skip_runaway and ((side < 0 and np.nanmax(zh[k0:i]) >= k_out) or (side > 0 and np.nanmin(zl[k0:i]) <= -k_out)):
                continue
            buf = stop_buf_atr * (atr[i] if np.isfinite(atr[i]) else 0)
            stp = seg_h + buf if side < 0 else seg_l - buf
            risk = abs(c[i] - stp)
            if risk <= 0 or risk > max_risk_pct / 100 * c[i]:
                continue
            if target == "35 pts":
                tg = c[i] + side * 35
            elif target == "1.5R":
                tg = c[i] + side * 1.5 * risk
            else:
                tg = m[i]
            reward = (tg - c[i]) * side
            if reward <= 0 or reward < min_rr * risk:
                continue
            used[side] = i
            count[dy] = count.get(dy, 0) + 1
            out.append({"i": i, "side": side, "entry": c[i], "stop": stp, "target": tg, "risk": risk,
                        "z": float(np.nanmax(zh[k0:i]) if side < 0 else np.nanmin(zl[k0:i]))})
            break
    return out, m, s, z


class MeanReversion(Indicator):
    id, name = "mean_reversion", "Mean Reversion (bands + snap-back)"
    overlay = False                    # z-score gets its own pane; mean, bands and arrows draw on price
    warmup = 120
    description = ("Mean line with ±σ bands and a z-score pane. Arrow when price stretched beyond the inner band and "
                   "a candle closes back inside against the stretch. Target = mean line. Practice tool; see ⚠ info.")
    params = [Param("center", "choice", "EMA", "Mean line", options=CENTERS, group="Bands"),
              Param("length", "int", 20, "Length", 5, 200, group="Bands"),
              Param("k_in", "float", 2.0, "Inner band (σ) — stretch", 0.5, 5, 0.1, group="Bands"),
              Param("k_out", "float", 3.0, "Outer band (σ) — extreme", 1, 6, 0.1, group="Bands"),
              Param("signals", "bool", True, "Show snap-back signals", group="Signals"),
              Param("lookback", "int", 3, "Close back inside within N bars", 1, 10, group="Signals"),
              Param("need_candle", "bool", True, "Signal candle must be the right colour", group="Signals"),
              Param("rsi_filter", "bool", False, "Require RSI extreme during the stretch", group="Signals"),
              Param("rsi_hi", "float", 70, "RSI overbought", 50, 95, group="Signals"),
              Param("rsi_lo", "float", 30, "RSI oversold", 5, 50, group="Signals"),
              Param("skip_runaway", "bool", False, "Skip if the outer band was hit (trend day)", group="Signals"),
              Param("target", "choice", "Mean line", "Target", options=TARGETS, group="Trade"),
              Param("min_rr", "float", 1.0, "Skip if reward < N × risk", 0, 5, 0.1, group="Trade"),
              Param("max_risk_pct", "float", 0.25, "Skip if stop wider than %", 0.05, 2, 0.05, group="Trade"),
              Param("skip_min", "int", 15, "No signal first N minutes", 0, 120, group="Trade"),
              Param("last_entry", "text", "14:30", "No signal after (HH:MM)", group="Trade"),
              Param("max_per_day", "int", 2, "Max signals per day", 1, 10, group="Trade"),
              Param("show_outer", "bool", True, "Show outer bands", group="Display"),
              Param("show_z", "bool", True, "Z-score pane", group="Display"),
              Param("show_sltp", "bool", True, "Draw SL / target lines", group="Display"),
              Param("mean_color", "color", "#ffb74d", "Mean colour", group="Display"),
              Param("band_color", "color", "#64b5f6", "Band colour", group="Display")]

    def compute(self, df, center, length, k_in, k_out, signals, lookback, need_candle, rsi_filter, rsi_hi, rsi_lo,
                skip_runaway, target, min_rr, max_risk_pct, skip_min, last_entry, max_per_day, show_outer, show_z,
                show_sltp, mean_color, band_color):
        t = times(df)
        n = len(df)
        sig, m, s, z = find_signals(df, center, length, k_in, k_out, lookback, need_candle, rsi_filter, 14,
                          rsi_hi, rsi_lo, skip_runaway, target, min_rr, max_risk_pct, skip_min,
                          last_entry, max_per_day)
        cname = center if not (center == "VWAP" and df.volume.sum() <= 0) else "Session mean (no volume)"
        series = [line("mean", f"Mean ({cname}{'' if center.startswith(('Session', 'VWAP')) else ' ' + str(length)})",
                       df, m, mean_color, pane=0, width=2),
                  line("up1", f"+{k_in:g}σ", df, m + k_in * s, band_color, pane=0, width=1),
                  line("dn1", f"−{k_in:g}σ", df, m - k_in * s, band_color, pane=0, width=1)]
        if show_outer:
            faint = "rgba(239,83,80,.55)"
            series += [line("up2", f"+{k_out:g}σ", df, m + k_out * s, faint, pane=0, width=1),
                       line("dn2", f"−{k_out:g}σ", df, m - k_out * s, faint, pane=0, width=1)]
        if show_z:
            cols = ["#ef5350" if v >= k_in else "#26a69a" if v <= -k_in else "rgba(144,164,174,.6)"
                    for v in np.nan_to_num(z)]
            series += [histogram("z", "Z-score", df, z, cols, pane=1),
                       line("zu", f"+{k_in:g}", df, np.full(n, k_in), "rgba(239,83,80,.5)", pane=1, width=1),
                       line("zd", f"−{k_in:g}", df, np.full(n, -k_in), "rgba(38,166,154,.5)", pane=1, width=1)]

        mk, shapes = [], []
        if signals:
            day = pd.factorize(df.index.normalize())[0]
            last_i = pd.Series(np.arange(n)).groupby(day).transform("last").to_numpy()
            for g in sig:
                i, up = g["i"], g["side"] > 0
                col = "#26a69a" if up else "#ef5350"
                mk.append({"time": int(t[i]), "position": "belowBar" if up else "aboveBar",
                           "shape": "arrowUp" if up else "arrowDown", "color": col,
                           "text": f"{'BUY' if up else 'SELL'} MR z{g['z']:+.1f}", "visible_from": int(t[i])})
                if show_sltp:
                    end = t[min(i + 10, last_i[i])]
                    shapes.append(seg(t[i], end, g["stop"], "#ef5350", f"SL {g['stop']:.0f}", 1, 1, t[i]))
                    shapes.append(seg(t[i], end, g["target"], "#26a69a", f"T {g['target']:.0f}", 1, 1, t[i]))

        info = {"Rules": f"price pokes beyond ±{k_in:g}σ, then within {lookback} bars a candle closes back inside → "
                         "fade toward the mean. SL beyond the stretch extreme.",
                "⚠ Backtest": "NIFTY 2015–Nov 2026, default rules, 5m & 15m: lost in every stage and every year "
                              "(PF 0.6–0.8, negative even before costs). Practice / context tool only."}
        if n and np.isfinite(z[-1]):
            info["Now"] = f"z = {z[-1]:+.2f} · mean {m[-1]:,.1f} · 1σ = {s[-1]:.1f} pts"
        if sig:
            g = sig[-1]
            info["Last signal"] = (f"{df.index[g['i']]:%d %b %H:%M} {'BUY' if g['side'] > 0 else 'SELL'} ≈ "
                                   f"{g['entry']:.1f} · SL {g['stop']:.1f} ({g['risk']:.0f} pts) · T {g['target']:.1f}")
        return Output(series=series, markers=mk, shapes=shapes, info=info)


INDICATORS = [MeanReversion()]
