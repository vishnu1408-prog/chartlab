"""Fake-breakout checker (NIFTY 5-minute). At the close of the FIRST 5m bar of the day that closes beyond a level
(previous-day high/low, 15-minute opening range high/low; 09:30–14:30) it shows the chance that price closes back
inside the level within 30 minutes (a "fake"). 30 minutes later it marks what happened (✓ held / ✗ fake).

Model: logistic regression on 3,199 NIFTY breakouts 2015–21 (indicators/tests/fake_breakout.py), features known at the
breakout close. Out of sample: AUC 0.68 (2022–25), 0.67 (2026); calibrated (says 20 % → 22 % were fake, 79 % → 77 %).
Strongest clue: how far the bar CLOSES beyond the level (in ATRs) — a close just past the level fails ~75 % of the time.
HONEST LIMIT: it predicts fakeouts, but neither trading the "real" ones nor fading the "fake" ones made money after
costs (2022–26, index points and ATM options). Use it to avoid chasing weak breakouts, not as a signal."""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from ... import store
from ..base import Indicator, Output, Param, rma, times, true_range
from .levels import seg

MODEL = json.loads((Path(__file__).parent / "fakeout_model.json").read_text())
LEVELS = (("PDH", 1), ("PDL", -1), ("ORH", 1), ("ORL", -1))


def fake_prob(f: dict) -> float:
    x = dict(f)
    x["minute"] = (x["minute"] - 570) / 300
    for name, _ in LEVELS:
        x[f"level_{name}"] = 1.0 if f["level"] == name else 0.0
    z = MODEL["intercept"] + sum(c * float(x[k]) for k, c in zip(MODEL["features"], MODEL["coef"]))
    return 1 / (1 + math.exp(-z))


class FakeBreakout(Indicator):
    id, name = "fakeout", "Fake breakout checker"
    description = ("Chance that a breakout of PDH / PDL / opening range closes back inside within 30 min. "
                   "Trained on NIFTY 5m 2015–21; predicts fakeouts, is not a profitable signal by itself.")
    warmup = 1700                                         # ≥ 21 sessions of 5m bars for the 20-day average range
    params = [Param("show_levels", "bool", True, "Draw the levels"),
              Param("show_result", "bool", True, "Mark the result after 30 min")]

    def compute(self, df, show_levels, show_result):
        if df.empty or len(df) < 100:
            return Output()
        tod = (df.index.hour * 60 + df.index.minute).to_numpy()
        step = np.diff(df.index.values).astype("timedelta64[m]").astype(int)
        if int(np.median(step[step > 0])) != 5:
            return Output(info={"note": "Works on the 5-minute chart only (the model was fitted on 5m bars)."})
        t = times(df)
        H, L, C, O = (df[k].to_numpy() for k in ("high", "low", "close", "open"))
        atr = rma(true_range(df), 14).shift(1).to_numpy()                       # known before the bar
        m15 = store.resample(df, "15")
        e15 = m15.close.ewm(span=20, adjust=False).mean()
        e15.index = e15.index + pd.Timedelta(minutes=15)                          # known when the 15m bar ends
        ema15 = e15.reindex(df.index + pd.Timedelta(minutes=5), method="ffill").to_numpy()
        day = df.index.normalize()
        D = df.groupby(day).agg(h=("high", "max"), l=("low", "min"), o=("open", "first"), c=("close", "last"))
        D["rng"] = D.h - D.l
        adr = D.rng.rolling(20).mean().shift(1)
        groups = pd.Series(np.arange(len(df)), index=df.index).groupby(day)
        shapes, markers, today = [], [], []
        days = list(D.index)
        for k in range(1, len(days)):
            d, prev = days[k], D.iloc[k - 1]
            ix = groups.get_group(d).to_numpy()
            if not np.isfinite(adr[d]) or adr[d] <= 0:
                continue
            orb = ix[(tod[ix] >= 555) & (tod[ix] < 570)]
            lv = {"PDH": prev.h, "PDL": prev.l}
            if len(orb) == 3:
                lv.update(ORH=H[orb].max(), ORL=L[orb].min())
            if show_levels:
                for name, price in lv.items():
                    vis = t[ix[0]] if name.startswith("PD") else t[orb[-1]]
                    shapes.append(seg(t[ix[0]] if name.startswith("PD") else t[orb[0]], t[ix[-1]], price,
                                      "#ff9800" if name.startswith("PD") else "#78909c", name, 1, 2, vis))
            gap = O[ix[0]] - prev.c
            for name, dirn in LEVELS:
                if name not in lv:
                    continue
                lvl, touches = lv[name], 0
                near = lambda j: (H[j] >= lvl * 0.9995) if dirn == 1 else (L[j] <= lvl * 1.0005)
                for j in ix:
                    if tod[j] < 570:
                        touches += near(j)
                        continue
                    if tod[j] > 870:
                        break
                    crossed = (C[j] > lvl >= C[j - 1]) if dirn == 1 else (C[j] < lvl <= C[j - 1])
                    if not crossed:
                        touches += near(j)
                        continue
                    if not np.isfinite(atr[j]) or atr[j] <= 0:
                        break
                    rng = H[j] - L[j]
                    f = dict(level=name, minute=tod[j], body_atr=abs(C[j] - O[j]) / atr[j], beyond_atr=abs(C[j] - lvl) / atr[j],
                             range_atr=rng / atr[j], wick_against=(((H[j] - C[j]) if dirn == 1 else (C[j] - L[j])) / rng) if rng > 0 else 0,
                             touches=touches, move_adr=(C[j] - O[ix[0]]) * dirn / adr[d], gap_with=float(np.sign(gap) == dirn),
                             with_trend=float((C[j] - ema15[j]) * dirn > 0) if np.isfinite(ema15[j]) else 0.5,
                             prev_rng_adr=prev.rng / adr[d])
                    p = fake_prob(f)
                    col = "#ef5350" if p >= 0.6 else ("#ffb300" if p >= 0.4 else "#26a69a")
                    why = "closed just past the level" if f["beyond_atr"] < 0.22 else ("closed well past the level" if f["beyond_atr"] > 0.55 else "")
                    markers.append({"time": int(t[j]), "position": "aboveBar" if dirn == 1 else "belowBar",
                                    "shape": "circle", "color": col, "visible_from": int(t[j]),
                                    "text": f"{name} {'↑' if dirn == 1 else '↓'} fake {round(p * 100)}%"})
                    nxt = [x for x in range(j + 1, min(j + 7, len(C))) if day[x] == d]
                    back = next((x for x in nxt if (C[x] < lvl if dirn == 1 else C[x] > lvl)), None)
                    if show_result and (back is not None or len(nxt) == 6):
                        x = back if back is not None else nxt[-1]
                        markers.append({"time": int(t[x]), "position": "belowBar" if dirn == 1 else "aboveBar", "shape": "square",
                                        "color": "#ef5350" if back is not None else "#26a69a", "visible_from": int(t[x]),
                                        "text": "✗ fake" if back is not None else "✓ held 30m"})
                    if k == len(days) - 1:
                        today.append(f"{name} {round(p * 100)}%" + (f" ({why})" if why else ""))
                    break
        a = MODEL["auc"]
        info = {"model": f"NIFTY 5m 2015–21 · out-of-sample AUC {a['2022-25']} / {a['2026']} · avg fake rate {round(MODEL['base_fake_rate'] * 100)}%",
                "read": "red ≥60% likely fake · amber 40–60% · green <40% · predicts fakeouts, not profit"}
        if today:
            info["today"] = ", ".join(today)
        return Output(shapes=shapes, markers=markers, info=info)


INDICATORS = [FakeBreakout()]
