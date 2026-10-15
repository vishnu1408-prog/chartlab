"""Indicator interface (built-in and custom indicators use the same one).

An indicator receives a DataFrame of bars (index = naive IST datetime, columns open/high/low/close/volume) and returns
an `Output`. Times in the output are epoch seconds from `t(...)` helpers below.

REPLAY CONTRACT (tested for every built-in): the value at bar i may only use bars <= i, and every marker / shape
carries `visible_from` = the time of the bar at whose CLOSE the object became known. During replay the chart shows only
objects with visible_from <= the replay cursor, so nothing from the future leaks.
"""
from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np
import pandas as pd

from ..store import to_epoch


@dataclass
class Param:
    name: str
    type: str = "int"                 # int | float | bool | choice | color | text
    default: Any = None
    label: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    options: list | None = None
    group: str = ""

    def coerce(self, v):
        if v is None:
            return self.default
        try:
            if self.type == "int":
                v = int(float(v))
            elif self.type == "float":
                v = float(v)
            elif self.type == "bool":
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif self.type == "choice":
                v = v if v in (self.options or []) else self.default
            else:
                v = str(v)
        except (TypeError, ValueError):
            return self.default
        if self.type in ("int", "float"):
            if self.min is not None:
                v = max(v, type(v)(self.min))
            if self.max is not None:
                v = min(v, type(v)(self.max))
        return v


@dataclass
class Output:
    series: list = field(default_factory=list)    # {id,title,type:line|histogram|area,pane:0|1..,color,data:[{time,value,color?}]}
    markers: list = field(default_factory=list)   # {time,position,shape,color,text,visible_from}
    shapes: list = field(default_factory=list)    # {kind,points:[{time,price}],color,width,dash,text,fill,visible_from,clip}
    info: dict = field(default_factory=dict)      # key → text (shown in the indicator's legend panel)

    def to_dict(self):
        return asdict(self)


class Indicator:
    id: str = ""
    name: str = ""
    description: str = ""
    overlay: bool = True              # drawn on the price pane (else in its own pane)
    params: list = []
    warmup: int = 300                 # extra bars loaded before the visible range so values are settled

    def compute(self, df: pd.DataFrame, **p) -> Output:  # pragma: no cover - abstract
        raise NotImplementedError

    def meta(self):
        return {"id": self.id, "name": self.name, "description": self.description, "overlay": self.overlay,
                "params": [asdict(x) for x in self.params]}

    def resolve(self, given: dict | None):
        given = given or {}
        return {x.name: x.coerce(given.get(x.name)) for x in self.params}


# ── helpers for indicator authors ──────────────────────────────────────────
def times(df: pd.DataFrame) -> np.ndarray:
    return to_epoch(df.index)


def line(id, title, df, values, color="#2962ff", pane=0, width=2, type="line"):
    t = times(df)
    v = np.asarray(values, dtype="float64")
    data = [{"time": int(t[i])} if not np.isfinite(v[i]) else {"time": int(t[i]), "value": float(v[i])} for i in range(len(v))]
    return {"id": id, "title": title, "type": type, "pane": pane, "color": color, "lineWidth": width, "data": data}


def histogram(id, title, df, values, colors, pane=1):
    t = times(df)
    v = np.asarray(values, dtype="float64")
    data = [{"time": int(t[i]), "value": float(v[i]), "color": colors[i]} if np.isfinite(v[i]) else {"time": int(t[i])}
            for i in range(len(v))]
    return {"id": id, "title": title, "type": "histogram", "pane": pane, "color": "#888", "data": data}


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df.close.shift(1)
    return pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
