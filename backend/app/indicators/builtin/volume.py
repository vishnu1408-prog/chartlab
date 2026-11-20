"""Volume pane: one bar per candle (green up / red down), the 'usual' volume for that time of day, high-volume bars
highlighted. NSE indices have no volume of their own: ChartLab fills it with the near-month future's volume in
contracts (app/futvol.py), so NIFTY shows NIFTY-futures volume.

Replay: a bar's value is shown once that candle has closed (frontend rule for every series). Live: the forming
candle's bar grows minute by minute; the legend reports the last CLOSED candle.
"""
import numpy as np

from ..base import Indicator, Output, Param, histogram, line
from .liquidity_zones import _fmt, _forming, rel_volume


class Volume(Indicator):
    id, name = "volume", "Volume"
    overlay = False
    warmup = 600                       # enough past days for the time-of-day average
    description = ("Volume per candle (NIFTY / BANKNIFTY: near-month futures, in contracts). Orange line = usual "
                   "volume for that time of day; bright bars = ≥ N × usual.")
    params = [Param("usual", "bool", True, "Show usual volume for the time of day"),
              Param("days", "int", 10, "Usual = average of the last N days", 3, 60),
              Param("hv_mult", "float", 1.8, "Highlight bars ≥ N × usual", 1.1, 5, 0.1),
              Param("up_color", "color", "#26a69a", "Up colour"),
              Param("down_color", "color", "#ef5350", "Down colour")]

    def compute(self, df, usual, days, hv_mult, up_color, down_color):
        v = df.volume.to_numpy(float)
        if not np.nansum(v) > 0:
            return Output(info={"note": "No volume for this symbol."})
        rel, avg = rel_volume(df, days)
        up = (df.close >= df.open).to_numpy()

        def rgba(hexc, a):
            r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5)) if hexc.startswith("#") and len(hexc) == 7 else (128, 128, 128)
            return f"rgba({r},{g},{b},{a})"
        cols = [rgba(up_color if u else down_color, 1.0 if (np.isfinite(x) and x >= hv_mult) else 0.45)
                for u, x in zip(up, rel)]
        series = [histogram("vol", "Volume", df, v, cols, pane=1)]
        if usual:
            series.append(line("usual", "Usual", df, avg, "#ff9800", pane=1, width=1))
        sym = df.attrs.get("symbol", "")
        info = {"Source": "near-month futures volume (contracts)" if sym in ("NIFTY 50", "NIFTY BANK") else "volume"}
        k = len(df) - 2 if _forming(df) else len(df) - 1
        if k >= 0:
            r = f" · {rel[k]:.1f}× usual" if np.isfinite(rel[k]) else ""
            info["Last closed candle"] = f"{df.index[k]:%H:%M} · {_fmt(v[k], True)}{r}"
        return Output(series=series, info=info)


INDICATORS = [Volume()]
