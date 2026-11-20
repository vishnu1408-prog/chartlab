"""Liquidity Zones: swing-high / swing-low zones with the volume (or touches) traded in them.

A swing high = the highest high of `length` bars before it and `right` bars after it (confirmed at the close of the
`right`-th candle after it; right=1 → known one candle later). Its zone runs from the
high down to the top of that candle's body (the wick area where sellers hit). Swing lows mirror it. The label shows the
volume traded inside the zone up to the moment the swing is confirmed (bars from the pivot through confirmation that
overlap the zone, counted from `length` bars before the pivot through confirmation). Symbols without volume (NSE indices) show the number of candles that touched the zone instead.

The zone extends right until a candle CLOSES through it (broken → zone stops, optional ✕ marker). A wick through the zone
that closes back inside is a "sweep" (optional ◆ marker) — the classic stop-hunt read.

Early mode: a candle whose high is the highest of the previous `length` candles is shown as a dashed "?" zone as
soon as it closes. It turns into a normal zone when the next `right` candles close without going beyond it, or is
removed at the close of the first candle that does (shapes carry visible_until; the chart hides them from then on).

Replay contract: zone, label and markers carry visible_from = the bar at whose close they became known; zone boxes are
clipped to the replay cursor, so the break point is not revealed early. Live: a still-forming candle is never used.
"""
import numpy as np
import pandas as pd

from ..base import Indicator, Output, Param, times


def _fmt(v, has_vol):
    if not has_vol:
        return f"{int(v)}×"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"{v / div:.1f}{suf}"
    return f"{v:.0f}"


def _forming(df, now=None):
    """True when the last bar has not closed yet (live data). Bar length = median spacing of the last bars."""
    if len(df) < 3:
        return False
    step = pd.Series(df.index[-20:]).diff().median()
    if pd.isna(step) or step <= pd.Timedelta(0):
        return False
    step = min(step, pd.Timedelta(days=1))
    now = now or (pd.Timestamp.now("UTC").tz_localize(None) + pd.Timedelta(hours=5, minutes=30))   # IST, naive
    return df.index[-1] + step > now


def find_zones(df, length=10, area="Wick", max_bars=300, same_day=False, right=None):
    """Return a list of zones: dict(side, i_piv, i_conf, top, bot, value, end, broken, sweeps[list of i])."""
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    v = df.volume.to_numpy(float) if "volume" in df else np.zeros(len(df))
    has_vol = np.nansum(v) > 0
    n = len(df)
    dcode = pd.factorize(df.index.normalize())[0]
    zones = []
    right = length if not right else right
    hmax_l = pd.Series(h).rolling(length + 1).max().to_numpy()          # bars p-length .. p
    lmin_l = pd.Series(l).rolling(length + 1).min().to_numpy()
    hmax_r = pd.Series(h[::-1]).rolling(right + 1).max().to_numpy()[::-1]   # bars p .. p+right
    lmin_r = pd.Series(l[::-1]).rolling(right + 1).min().to_numpy()[::-1]
    for p in range(length, n - right):
        for side in (1, -1):                      # 1 = swing high, -1 = swing low
            if side > 0 and not (h[p] == hmax_l[p] == hmax_r[p] and h[p] > h[p - 1]):
                continue
            if side < 0 and not (l[p] == lmin_l[p] == lmin_r[p] and l[p] < l[p - 1]):
                continue
            conf = p + right
            if same_day and dcode[conf] != dcode[p]:
                continue
            if side > 0:
                top, bot = h[p], (max(o[p], c[p]) if area == "Wick" else l[p])
            else:
                top, bot = (min(o[p], c[p]) if area == "Wick" else h[p]), l[p]
            if top - bot <= 0:
                top, bot = (top, top - 1e-6) if side > 0 else (bot + 1e-6, bot)
            seg = slice(p - length, conf + 1)
            touch = (h[seg] >= bot) & (l[seg] <= top)
            value = float(v[seg][touch].sum()) if has_vol else float(touch.sum())
            end, broken, sweeps = min(n - 1, conf + max_bars), False, []
            for k in range(conf + 1, min(n, conf + max_bars + 1)):
                if (side > 0 and c[k] > top) or (side < 0 and c[k] < bot):
                    end, broken = k, True
                    break
                if (side > 0 and h[k] > top) or (side < 0 and l[k] < bot):
                    sweeps.append(k)
            zones.append(dict(side=side, i_piv=p, i_conf=conf, top=top, bot=bot, value=value, end=end,
                              broken=broken, sweeps=sweeps))
    return zones, has_vol


def rel_volume(df, days=10):
    """Volume ÷ the average volume of the same time-of-day slot over the previous `days` sessions (past only)."""
    v = df.volume.astype("float64")
    slot = df.index.hour * 60 + df.index.minute
    usual = v.groupby(slot).transform(lambda x: x.shift(1).rolling(days, min_periods=max(3, days // 2)).mean())
    return (v / usual.replace(0, np.nan)).to_numpy(), usual.to_numpy()


def _hv_marks(df, t, length, mult, days, show, high_color, low_color):
    """At a candle's close: new `length`-candle high (low) on volume ≥ mult × usual → 'HV' mark. Never withdrawn."""
    h, l = df.high.to_numpy(float), df.low.to_numpy(float)
    v = df.volume.to_numpy(float)
    rel, _ = rel_volume(df, days)
    hmax = pd.Series(h).rolling(length + 1).max().to_numpy()
    lmin = pd.Series(l).rolling(length + 1).min().to_numpy()
    out = []
    for p in range(length, len(df)):
        if not (np.isfinite(rel[p]) and rel[p] >= mult):
            continue
        up = h[p] == hmax[p] and h[p] > h[p - 1] and show != "Lows"
        dn = l[p] == lmin[p] and l[p] < l[p - 1] and show != "Highs"
        if up == dn:                               # neither, or an outside bar making both → skip
            continue
        out.append({"time": int(t[p]), "position": "aboveBar" if up else "belowBar",
                    "shape": "arrowDown" if up else "arrowUp", "color": high_color if up else low_color,
                    "text": f"HV {_fmt(v[p], True)} · {rel[p]:.1f}×", "visible_from": int(t[p])})
    return out


def _early_shapes(df, t, length, right, area, show, has_vol, high_color, low_color, rgb, only=None):
    """Dashed '?' zones from the swing candle's close until confirmed (next `right` candles stay inside) or cancelled."""
    o, h, l, c = (df[x].to_numpy(float) for x in ("open", "high", "low", "close"))
    v = df.volume.to_numpy(float) if "volume" in df else np.zeros(len(df))
    n = len(df)
    right = length if not right else right
    hmax_l = pd.Series(h).rolling(length + 1).max().to_numpy()
    lmin_l = pd.Series(l).rolling(length + 1).min().to_numpy()
    r, g, b = rgb
    out = []
    for p in range(length, n):
        if only is not None and not only[p]:              # e.g. only high-volume candles
            continue
        for side in (1, -1):
            if show == "Highs" and side < 0 or show == "Lows" and side > 0:
                continue
            if side > 0 and not (h[p] == hmax_l[p] and h[p] > h[p - 1]):
                continue
            if side < 0 and not (l[p] == lmin_l[p] and l[p] < l[p - 1]):
                continue
            if side > 0:
                top, bot = h[p], (max(o[p], c[p]) if area == "Wick" else l[p])
            else:
                top, bot = (min(o[p], c[p]) if area == "Wick" else h[p]), l[p]
            if top - bot <= 0:
                top, bot = (top, top - 1e-6) if side > 0 else (bot + 1e-6, bot)
            until = None
            for k in range(p + 1, min(n, p + right + 1)):
                if (side > 0 and h[k] > top) or (side < 0 and l[k] < bot):
                    until = k                         # cancelled at this candle's close
                    break
            if until is None and p + right <= n - 1:
                until = p + right                     # confirmed → the normal zone takes over
            seg = slice(p - length, p + 1)
            touch = (h[seg] >= bot) & (l[seg] <= top)
            value = float(v[seg][touch].sum()) if has_vol else float(touch.sum())
            end = int(t[until]) if until is not None else int(t[min(len(t) - 1, n)])
            vis = int(t[p])
            extra = {"visible_from": vis}
            if until is not None:
                extra["visible_until"] = int(t[until])
            col = high_color if side > 0 else low_color
            out.append({"kind": "box", "points": [{"time": vis, "price": float(top)}, {"time": end, "price": float(bot)}],
                        "color": f"rgba({r},{g},{b},.7)", "width": 1, "dash": 2, "text": "",
                        "fill": f"rgba({r},{g},{b},.06)", "clip": True, **extra})
            out.append({"kind": "label", "points": [{"time": vis, "price": float(top if side > 0 else bot)}],
                        "color": col, "text": _fmt(value, has_vol) + "?", **extra})
    return out


class LiquidityZones(Indicator):
    id, name = "liquidity_zones", "Liquidity Zones (swing volume)"
    warmup = 200
    description = ("Swing-high / swing-low zones with the volume traded in them (touch count when the symbol has no "
                   "volume, e.g. NSE indices). Zones stop when a candle closes through; optional sweep / break marks.")
    params = [Param("length", "int", 5, "Swing length (bars before)", 2, 50, group="Zones"),
              Param("right", "int", 0, "Confirm after N candles (0 = same as length)", 0, 50, group="Zones"),
              Param("early", "choice", "HV candles only", "Early zone at the swing candle's close (dashed '?', "
                    "removed if the next candle goes beyond)", options=["Off", "HV candles only", "All candidates"],
                    group="Zones"),
              Param("hv", "bool", False, "Also mark high-volume swing candles with an arrow (HV)", group="Volume"),
              Param("hv_mult", "float", 1.8, "HV = volume ≥ N × usual for that time of day", 1.1, 5, 0.1,
                    group="Volume"),
              Param("hv_days", "int", 10, "Usual volume = average of the last N days", 3, 60, group="Volume"),
              Param("area", "choice", "Wick", "Zone area", options=["Wick", "Full candle"], group="Zones"),
              Param("show", "choice", "Both", "Show", options=["Both", "Highs", "Lows"], group="Zones"),
              Param("min_pct", "int", 0, "Hide zones below N % of the biggest (last 50)", 0, 90, group="Zones"),
              Param("max_bars", "int", 300, "Max zone length (bars)", 10, 3000, group="Zones"),
              Param("same_day", "bool", False, "Swing must confirm on the same day", group="Zones"),
              Param("sweeps", "bool", True, "Mark sweeps (wick through, close back)", group="Marks"),
              Param("breaks", "bool", False, "Mark breaks (close through)", group="Marks"),
              Param("high_color", "color", "#ef5350", "Swing-high colour", group="Display"),
              Param("low_color", "color", "#26a69a", "Swing-low colour", group="Display"),
              Param("fill", "color", "#ba68c8", "Zone fill", group="Display")]

    def compute(self, df, length, right, early, hv, hv_mult, hv_days, area, show, min_pct, max_bars, same_day, sweeps, breaks, high_color,
                low_color, fill):
        t = times(df)
        closed = df.iloc[:-1] if _forming(df) else df      # live: never confirm or mark on a still-forming candle
        zones, has_vol = find_zones(closed, length, area, max_bars, same_day, right)
        last = len(df) - 1
        for z in zones:                                     # open zones keep extending to the newest candle
            if not z["broken"] and z["end"] == len(closed) - 1:
                z["end"] = last
        if show == "Highs":
            zones = [z for z in zones if z["side"] > 0]
        elif show == "Lows":
            zones = [z for z in zones if z["side"] < 0]
        if min_pct > 0 and zones:                 # relative filter uses only zones confirmed earlier (no look-ahead)
            vals, keep = [], []
            for z in zones:
                ref = max(vals[-50:]) if vals else 0
                if not vals or z["value"] >= min_pct / 100 * ref:
                    keep.append(z)
                vals.append(z["value"])
            zones = keep
        r, g, b = (int(fill[i:i + 2], 16) for i in (1, 3, 5)) if fill.startswith("#") and len(fill) == 7 else (186, 104, 200)
        shapes, mk = [], []
        for z in zones:
            col = high_color if z["side"] > 0 else low_color
            vis = int(t[z["i_conf"]])
            shapes.append({"kind": "box", "points": [{"time": int(t[z["i_piv"]]), "price": float(z["top"])},
                                                     {"time": int(t[z["end"]]), "price": float(z["bot"])}],
                           "color": f"rgba({r},{g},{b},.55)", "width": 1, "dash": 0, "text": "",
                           "fill": f"rgba({r},{g},{b},.13)", "visible_from": vis, "clip": True})
            mid = (z["top"] + z["bot"]) / 2
            shapes.append({"kind": "segment", "points": [{"time": int(t[z["i_piv"]]), "price": float(mid)},
                                                         {"time": int(t[z["end"]]), "price": float(mid)}],
                           "color": f"rgba({r},{g},{b},.6)", "width": 1, "dash": 2, "text": "", "visible_from": vis,
                           "clip": True})
            lab_p = z["top"] if z["side"] > 0 else z["bot"]
            shapes.append({"kind": "label", "points": [{"time": int(t[z["i_piv"]]), "price": float(lab_p)}],
                           "color": col, "text": _fmt(z["value"], has_vol), "visible_from": vis})
            if sweeps:
                for k in z["sweeps"][:1]:                 # first sweep of each zone only
                    mk.append({"time": int(t[k]), "position": "aboveBar" if z["side"] > 0 else "belowBar",
                               "shape": "circle", "color": "#ffca28", "text": "sweep", "visible_from": int(t[k])})
            if breaks and z["broken"]:
                k = z["end"]
                mk.append({"time": int(t[k]), "position": "belowBar" if z["side"] > 0 else "aboveBar",
                           "shape": "square", "color": col, "text": "break", "visible_from": int(t[k])})
        if hv and has_vol:
            mk += _hv_marks(closed, t, length, hv_mult, hv_days, show, high_color, low_color)
        if early == "All candidates" or (early == "HV candles only" and has_vol):
            only = None
            if early == "HV candles only":
                rel, _ = rel_volume(closed, hv_days)
                only = np.nan_to_num(rel) >= hv_mult
            shapes += _early_shapes(closed, t, length, right, area, show, has_vol, high_color, low_color, (r, g, b),
                                    only)
        mk.sort(key=lambda m: m["time"])
        info = {"Label": (("volume traded in the zone (pivot → confirmation)" +
                           (" — NIFTY futures contracts" if df.attrs.get("symbol") in ("NIFTY 50", "NIFTY BANK") else ""))
                          if has_vol else
                          "no volume for this symbol → number of candles that touched the zone"),
                "How to read": "bigger number = more trading at that swing → more stops resting beyond it. Wick "
                               "through + close back = sweep (stop hunt); close through = break. Untested on NIFTY — "
                               "context only."}
        open_z = [z for z in zones if not z["broken"]]
        if open_z:
            z = open_z[-1]
            info["Latest open zone"] = (f"{'High' if z['side'] > 0 else 'Low'} {z['bot']:,.1f}–{z['top']:,.1f} · "
                                        f"{_fmt(z['value'], has_vol)}")
        return Output(markers=mk, shapes=shapes, info=info)


INDICATORS = [LiquidityZones()]
