"""NG opening drive (MCX natural gas, 5-minute) — a FORWARD TEST, not a proven strategy.

Rule (indicators/tests/gann_markets.py "pct" level, pre-registered 2026-10-13; natural gas added 2026-10-17):
  drive  : the session's FIRST 5m bar must start at the scheduled open (09:00; 10:00 before 2019; 17:00 on evening-only
           holiday sessions) and its body |close − open| ≥ 0.25 × ADR10 (average high−low of the last 10 sessions)
  signal : the first 5m close from the 2nd bar (until 22:00) beyond open ± 0.33 % → in practice always the 2nd bar, in
           the first bar's direction ("strong first-bar continuation")
  trade  : enter at the next bar's open (≈ 09:10); target = stop = 0.15 × ADR10 (5m bars, stop first); out at 23:00
  costs  : ₹0.24 per mmBtu all-in (brokerage + charges on the 1,250 lot + ~1 tick slippage each way)
Validator (2026-10-17) — WEAK: clean (no look-ahead, rolls don't create fake drives, results smooth in the parameters),
but after realistic costs 2022+ ≈ +0.18 R (t ≈ 2.1), the 2022+ edge sits mostly on MONDAYS (Tue–Fri ≈ +0.14 R, t 1.0),
the last months are flat/negative, and NATGASMINI's per-order costs remove it. Gold and silver LOSE with this rule.
Judge it only on the live tally from 19 Oct 2026: ≥ 30 trades with average ≥ +0.10 R after costs.
"""
import numpy as np
import pandas as pd

from ..base import Indicator, Output, Param, times, to_epoch
from .levels import seg

SYM = "MCX NATURALGAS"
LIVE_FROM = pd.Timestamp("2026-10-19")
K_DRIVE, K_EXIT, LVL = 0.25, 0.15, 0.0033
COST_PER_UNIT = 0.24                                      # ₹ per mmBtu, round trip, all-in
OPENS = (540, 600, 1020)
BACKTEST = ("realistic costs: ≤2021 +0.13 R · 2022–Oct26 +0.18 R (t≈2.1), ~2.6 trades/month · Mondays carry it "
            "(2022+ Mon +0.39 R gross vs Tue–Fri +0.14 R) · validator: WEAK — forward test")


def ng_trades(df5: pd.DataFrame, cost: float = COST_PER_UNIT) -> list:
    """One row per session with a strong first bar. Pure (used by the indicator, the bot and the tests)."""
    if df5.empty:
        return []
    tod = (df5.index.hour * 60 + df5.index.minute).to_numpy()
    O, H, L, C = (df5[k].to_numpy() for k in ("open", "high", "low", "close"))
    day = df5.index.normalize()
    D = df5.groupby(day).agg(h=("high", "max"), l=("low", "min"))
    adr = (D.h - D.l).rolling(10).mean().shift(1)
    grp = pd.Series(np.arange(len(df5)), index=df5.index).groupby(day)
    out = []
    for d in D.index:
        ix = grp.get_group(d).to_numpy()
        a = adr.get(d, np.nan)
        if tod[ix[0]] not in OPENS or not np.isfinite(a) or a <= 0:
            continue
        o = O[ix[0]]
        body = C[ix[0]] - o
        row = dict(day=d, ix=ix, open=o, drive=abs(body) / a, adr=a, strong=abs(body) >= K_DRIVE * a)
        if not row["strong"]:
            out.append(row)
            continue
        for j in ix[1:]:
            if tod[j] > 22 * 60:
                break
            dirn = 1 if C[j] > o * (1 + LVL) else -1 if C[j] < o * (1 - LVL) else 0
            if not dirn:
                continue
            row.update(sig=j, dir=dirn)
            if j + 1 <= ix[-1]:
                k = j + 1
                e = O[k]
                risk = K_EXIT * a
                s, t = e - dirn * risk, e + dirn * risk
                ex = None
                for m in ix[ix >= k]:
                    if tod[m] >= 23 * 60:
                        ex = (m, O[m], "23:00")
                        break
                    if (L[m] <= s) if dirn == 1 else (H[m] >= s):
                        ex = (m, min(O[m], s) if dirn == 1 else max(O[m], s), "stop")
                        break
                    if (H[m] >= t) if dirn == 1 else (L[m] <= t):
                        ex = (m, t, "target")
                        break
                row.update(entry_i=k, entry=e, risk=risk, stop=s, target=t)
                if ex is not None:
                    pts = (ex[1] - e) * dirn - cost
                    row.update(exit_i=ex[0], exit_t=df5.index[ex[0]], exit=ex[1], why=ex[2], pts=pts, R=pts / risk)
            break
        out.append(row)
    return out


def tally(rows) -> str:
    done = [r for r in rows if r.get("strong") and "R" in r]
    if not done:
        return "no trades yet"
    R = np.array([r["R"] for r in done])
    mon = np.array([r["R"] for r in done if r["day"].dayofweek == 0])
    rest = np.array([r["R"] for r in done if r["day"].dayofweek != 0])
    part = lambda x: f"{len(x)} · {x.mean():+.2f} R" if len(x) else "0"
    return (f"{len(R)} trades · win {round(100 * (R > 0).mean())}% · avg {R.mean():+.2f} R · total {R.sum():+.1f} R "
            f"(Mon {part(mon)} | Tue–Fri {part(rest)})")


class NGDrive(Indicator):
    id, name = "ng_drive", "NG opening drive (forward test)"
    description = ("MCX natural gas: a strong first 5-minute bar (≥ 0.25×ADR10) → follow it from the next bar, "
                   "target = stop = 0.15×ADR10, out 23:00. Validator: WEAK — paper forward test from 19 Oct 2026.")
    warmup = 3000
    params = [Param("show_skips", "bool", False, "Mark weak-drive days (no trade)")]

    def compute(self, df, show_skips):
        if df.empty or len(df) < 100:
            return Output()
        if df.attrs.get("symbol") not in (None, SYM):
            return Output(info={"note": "Built for MCX NATURALGAS only — gold and silver lose with this rule; crude "
                                        "has its own Gann drive mode."})
        step = np.diff(df.index.values).astype("timedelta64[m]").astype(int)
        if int(np.median(step[step > 0])) != 5:
            return Output(info={"note": "Works on the 5-minute chart only."})
        rows = ng_trades(df)
        t = times(df)
        shapes, markers = [], []
        for r in rows:
            ix = r["ix"]
            if not r["strong"]:
                if show_skips:
                    markers.append({"time": int(t[ix[0]]), "position": "aboveBar", "shape": "circle", "color": "#78909c",
                                    "visible_from": int(t[ix[0]]), "text": f"NG skip · drive {r['drive']:.2f}×ADR"})
                continue
            if "sig" not in r:
                continue
            d, j = r["dir"], r["sig"]
            markers.append({"time": int(t[j]), "position": "belowBar" if d == 1 else "aboveBar",
                            "shape": "arrowUp" if d == 1 else "arrowDown", "color": "#26a69a" if d == 1 else "#ef5350",
                            "visible_from": int(t[j]), "text": f"NG {'BUY' if d == 1 else 'SELL'} · drive {r['drive']:.2f}×ADR"})
            if "entry_i" in r:
                ei = r["entry_i"]
                end = r.get("exit_i", ix[-1])
                shapes.append(seg(t[ei], t[end], r["target"], "#26a69a", "T", 1, 1, t[ei]))
                shapes.append(seg(t[ei], t[end], r["stop"], "#ef5350", "SL", 1, 1, t[ei]))
            if "R" in r:
                ok = r["R"] > 0
                vis = int(to_epoch(pd.DatetimeIndex([r["exit_t"]]))[0])
                markers.append({"time": int(t[r["exit_i"]]), "position": "aboveBar" if d == 1 else "belowBar",
                                "shape": "square", "color": "#26a69a" if ok else "#ef5350", "visible_from": vis,
                                "text": f"{'✓' if ok else '✗'} {r['why']} {r['R']:+.2f}R"})
        live = [r for r in rows if r["day"] >= LIVE_FROM]
        n_live = len([r for r in live if "R" in r])
        info = {"rules": f"first bar at the open with body ≥ {K_DRIVE}×ADR10 → follow it from the next bar · target = stop "
                         f"= {K_EXIT}×ADR10 · 23:00 exit · costs ₹{COST_PER_UNIT}/mmBtu",
                "backtest": BACKTEST,
                "on this chart": tally(rows),
                "live since 19 Oct 2026": tally(live) + f" · pass mark: ≥30 trades and avg ≥ +0.10 R ({n_live}/30)"}
        last = rows[-1] if rows else None
        if last is not None and last["day"] == df.index[-1].normalize():
            s = f"open {last['open']:.1f} · drive {last['drive']:.2f}×ADR · "
            if not last["strong"]:
                s += "weak → no trade today"
            elif "R" in last:
                s += f"{'BUY' if last['dir'] == 1 else 'SELL'} {last['why']} {last['R']:+.2f} R"
            elif "entry" in last:
                s += f"{'BUY' if last['dir'] == 1 else 'SELL'} open from {last['entry']:.1f} (stop {last['stop']:.1f}, " \
                     f"target {last['target']:.1f})"
            else:
                s += "strong → waiting for the next bar"
            info["today"] = s
        return Output(shapes=shapes, markers=markers, info=info)


INDICATORS = [NGDrive()]
