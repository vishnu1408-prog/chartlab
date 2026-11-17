"""Economic-calendar markers: RBI, India CPI, Budget, FOMC, US data (CPI, jobs …), expiry days, EIA reports (MCX).

Each event sits on the candle in which it happens; events outside the session (e.g. US data at night on a NIFTY chart)
go on the next session's first candle, with their real time in the label. High-impact events also get a dashed vertical
line. Scheduled events are known in advance, so they show in replay too. Upcoming events are listed in the legend.
"""
import datetime as dt

import numpy as np
import pandas as pd

from ..base import Indicator, Output, Param, times

COL = {"rbi": "#ff9800", "budget": "#ff9800", "cpi": "#ffb74d", "fomc": "#e040fb", "nfp": "#ab47bc", "macro": "#ba68c8",
       "expiry": "#29b6f6", "eia": "#26c6da", "custom": "#ffeb3b"}
FLAG = {"IN": "🇮🇳", "USD": "🇺🇸", "MCX": "⛽", "CNY": "🇨🇳", "All": "🌐"}


class EventMarkers(Indicator):
    id, name = "events", "Events (economic calendar)"
    description = ("RBI policy, India CPI, Budget, US Fed / CPI / jobs report, expiry days and (MCX) EIA inventory "
                   "reports as markers, high-impact ones with a line; upcoming events in the legend.")
    warmup = 0
    params = [Param("india", "bool", True, "India: RBI, CPI, Budget"), Param("us", "bool", True, "US / global data, Fed"),
              Param("expiry", "bool", True, "Expiry days"), Param("eia", "bool", True, "EIA oil / gas reports (MCX)"),
              Param("impact", "choice", "High", "Minimum impact", options=["High", "Medium"]),
              Param("lines", "bool", True, "Vertical line on high-impact events")]

    def compute(self, df, india, us, expiry, eia, impact, lines):
        from ... import events as ev
        if df.empty:
            return Output()
        sym = df.attrs.get("symbol") or ""
        mcx, btc = sym.startswith("MCX "), sym.startswith("BTC")
        markets = set()
        if india and not btc:
            markets.add("in")
        if us:
            markets.add("us")
        if expiry and not btc:
            markets.add("mcx" if mcx else "nse")
        if eia and mcx:
            markets.add("eia")
        start = df.index[0].to_pydatetime()
        end = df.index[-1].to_pydatetime() + dt.timedelta(days=10)
        try:
            ev.refresh()
            E = ev.events(start, end, markets, impact)
        except Exception as e:
            return Output(info={"error": f"calendar: {e}"})
        if mcx:                                            # NIFTY expiries are irrelevant on MCX charts and vice versa
            E = [e for e in E if not (e["category"] == "expiry" and e["country"] == "IN")]
        t = times(df)
        idx = df.index
        lo, hi = float(df.low.min()), float(df.high.max())
        from ...sync import now_ist
        now = pd.Timestamp(now_ist())
        markers, shapes = [], []
        upcoming = [e for e in E if pd.Timestamp(e["ts"]) > now]
        last = idx[-1]
        for e in E:
            ts = pd.Timestamp(e["ts"])
            if ts > last + pd.Timedelta(minutes=5):
                continue
            i = int(idx.searchsorted(ts, side="right")) - 1
            if i < 0 or idx[i].normalize() != ts.normalize() or (ts - idx[i]) > pd.Timedelta(hours=1):
                i = int(idx.searchsorted(ts))               # outside the session → the next candle
                if i >= len(idx):
                    continue
            bt = int(t[i])
            col = COL.get(e["category"], "#bdbdbd")
            label = f"{FLAG.get(e['country'], '')} {e['title']} {e['ts']:%d %b %H:%M}"
            markers.append({"time": bt, "position": "aboveBar", "shape": "square", "color": col, "text": label,
                            "visible_from": bt - 30 * 86400})
            if lines and e.get("impact") == "High":
                shapes.append({"kind": "segment", "points": [{"time": bt, "price": hi}, {"time": bt, "price": lo}],
                               "color": col, "width": 1, "dash": 2, "text": "", "visible_from": bt - 30 * 86400})
        info = {"shown": f"{len(markers)} events on the chart"}
        if upcoming:
            info["next"] = " · ".join(f"{FLAG.get(e['country'], '')} {e['title']} {e['ts']:%a %d %b %H:%M}"
                                      for e in upcoming[:4])
        return Output(markers=markers, shapes=shapes, info=info)


INDICATORS = [EventMarkers()]
