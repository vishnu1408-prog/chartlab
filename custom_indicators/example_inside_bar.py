"""Example custom indicator — copy this file, change it, save: it shows up in ChartLab's Indicators menu
(no restart needed). Marks inside bars and their breakout, plus a fast/slow EMA ribbon."""
import numpy as np

from app.indicators.base import Indicator, Output, Param, ema, line, times


class InsideBarBreakout(Indicator):
    id = "inside_bar"                      # unique id (letters, digits, _)
    name = "Inside Bar Breakout (example)"
    description = "Example custom indicator: inside bars + first close outside the mother bar."
    overlay = True                         # False → own pane below the chart
    params = [
        Param("fast", "int", 9, "Fast EMA", 1, 200),
        Param("slow", "int", 21, "Slow EMA", 2, 400),
        Param("show_ema", "bool", True, "Show EMAs"),
    ]

    def compute(self, df, fast, slow, show_ema):
        h, l, c = df.high.to_numpy(), df.low.to_numpy(), df.close.to_numpy()
        t = times(df)
        out = Output()
        if show_ema:
            out.series += [line("fast", f"EMA {fast}", df, ema(df.close, fast), "#00bcd4", width=1),
                           line("slow", f"EMA {slow}", df, ema(df.close, slow), "#ff9800", width=1)]
        mother = None
        for i in range(1, len(df)):
            if h[i] <= h[i - 1] and l[i] >= l[i - 1]:                 # inside bar → remember the mother bar
                mother = (i - 1, h[i - 1], l[i - 1])
                out.shapes.append({"kind": "box", "points": [{"time": int(t[i - 1]), "price": float(h[i - 1])},
                                                              {"time": int(t[i]), "price": float(l[i - 1])}],
                                   "color": "#787b86", "fill": "#787b8622", "width": 1, "visible_from": int(t[i])})
            elif mother and (c[i] > mother[1] or c[i] < mother[2]):   # first close outside the mother bar
                up = c[i] > mother[1]
                out.markers.append({"time": int(t[i]), "position": "belowBar" if up else "aboveBar",
                                    "shape": "arrowUp" if up else "arrowDown", "color": "#26a69a" if up else "#ef5350",
                                    "text": "IB↑" if up else "IB↓", "visible_from": int(t[i])})
                mother = None
        return out
