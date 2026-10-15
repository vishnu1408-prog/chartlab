# Custom indicators

Drop a `.py` file in this folder and it appears in ChartLab's **ƒx Indicators** menu under *Custom*. Saving the file
again reloads it (no restart). Files starting with `_` are ignored. If a file has an error, the menu shows it and the rest
of the app keeps working.

```python
from app.indicators.base import Indicator, Output, Param, ema, line, histogram, times

class MyIndicator(Indicator):
    id = "my_indicator"            # unique
    name = "My Indicator"
    overlay = True                 # False = own pane below the price chart
    params = [Param("length", "int", 20, "Length", 1, 500)]

    def compute(self, df, length):          # df: index = IST datetime, columns open high low close volume
        out = Output()
        out.series.append(line("ma", f"MA {length}", df, df.close.rolling(length).mean(), "#f5a623"))
        return out
```

What you can return in `Output`:

| field | content |
|---|---|
| `series` | lines / histograms (`line(...)`, `histogram(...)`); `pane=0` price chart, `pane=1` the indicator's own pane |
| `markers` | `{time, position: aboveBar/belowBar/inBar, shape: arrowUp/arrowDown/circle/square, color, text, visible_from}` |
| `shapes` | `{kind: segment/box/polygon/polyline/label, points: [{time, price}], color, width, dash, fill, text, visible_from, clip}` |
| `info` | `{"label": "text"}` shown under the indicator's name |

Param types: `int`, `float`, `bool`, `choice` (with `options=[...]`), `color`, `text`. Use `group="..."` to group them in
the settings dialog.

**Replay rule (important):** the value at a bar may only use that bar and earlier bars, and every marker/shape must set
`visible_from` to the time of the bar at whose close it became known (`times(df)[i]`). Replay hides everything with a
later `visible_from`, so a correct indicator never shows the future. `clip: True` on a shape makes its right end stop at
the replay cursor (e.g. a level drawn until 15:15). `live_only: True` hides a shape during replay.

Times: `times(df)` gives the epoch seconds the chart uses (IST wall-clock time).
