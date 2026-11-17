"""Previous-day 50 % level + PDH / PDL breakouts (user's ideas, tested 2026-10-25 — they LOSE money except one corner).

  level : M = (previous day's high + low) / 2  (= PDL + (PDH − PDL) / 2)
  CALL  : a closed 3/5/15-minute candle opening ABOVE M whose wick touches M (low ≤ M) and closes above M → buy next open
  PUT   : a closed candle opening BELOW M whose wick touches M (high ≥ M) and closes below M → sell next open
  breakouts (option): first close ABOVE the previous day's high → CALL; first close BELOW its low → PUT (once each a day)
  exit  : NIFTY target 30 / stop 30 points; MCX natural gas / crude / gold target = stop = 0.09 × ADR10; checked on
          1-minute bars (both in one minute → stop); out 15:15 (NIFTY) / 23:00 (MCX). One trade at a time.
Backtests 2015/16 – Oct 2026 after costs: NIFTY, crude and gold lose with every part and timeframe; natural gas PDL
breakdowns +0.05…+0.09 R (8–9 of 10 years up) but only ≈ ₹150–260 a month per mini lot — less than 1 tick of slippage.
"""
import numpy as np
import pandas as pd

from ... import store, symbols
from ..base import Indicator, Output, Param, times
from .levels import seg

COST, LOT = 0.0002, 65
MCX = {"MCX NATURALGAS", "MCX CRUDEOIL", "MCX GOLD"}
# backtest summaries (first signal of the day, after costs) — indicators/tests/mid_level.py and the 2026-10-25 runs
# backtests 2015/16 – Oct 2026, first 50 % signal + PDH/PDL breakouts, after costs (2026-10-25 run, per lot)
BT = {
    ('MCX CRUDEOIL', 3): {'50%': '50%: 1299 trades, win 51%, -0.11 R net, ₹-132/month, 2025 ₹-2,734, yrs up 2/10', 'PDH break': 'PDH break: 1234 trades, win 55%, -0.04 R net, ₹+2/month, 2025 ₹+846, yrs up 2/10', 'PDL break': 'PDL break: 1016 trades, win 51%, -0.10 R net, ₹-103/month, 2025 ₹-3,441, yrs up 1/10', 'ALL': 'ALL: 3549 trades, win 52%, -0.08 R net, ₹-233/month, 2025 ₹-5,329, yrs up 1/10'},
    ('MCX CRUDEOIL', 5): {'50%': '50%: 1302 trades, win 50%, -0.13 R net, ₹-201/month, 2025 ₹-1,885, yrs up 0/10', 'PDH break': 'PDH break: 1214 trades, win 55%, -0.03 R net, ₹+15/month, 2025 ₹-536, yrs up 2/10', 'PDL break': 'PDL break: 1010 trades, win 53%, -0.08 R net, ₹-79/month, 2025 ₹-2,181, yrs up 2/10', 'ALL': 'ALL: 3526 trades, win 52%, -0.08 R net, ₹-265/month, 2025 ₹-4,603, yrs up 2/10'},
    ('MCX CRUDEOIL', 15): {'50%': '50%: 1263 trades, win 51%, -0.10 R net, ₹-144/month, 2025 ₹-994, yrs up 1/10', 'PDH break': 'PDH break: 1183 trades, win 55%, -0.04 R net, ₹+12/month, 2025 ₹+223, yrs up 3/10', 'PDL break': 'PDL break: 971 trades, win 50%, -0.12 R net, ₹-119/month, 2025 ₹-2,937, yrs up 2/10', 'ALL': 'ALL: 3417 trades, win 52%, -0.09 R net, ₹-251/month, 2025 ₹-3,708, yrs up 1/10'},
    ('MCX GOLD', 3): {'50%': '50%: 1170 trades, win 50%, -0.36 R net, ₹-1,687/month, 2025 ₹-36,960, yrs up 0/10', 'PDH break': 'PDH break: 1179 trades, win 55%, -0.34 R net, ₹-1,080/month, 2025 ₹-13,833, yrs up 0/10', 'PDL break': 'PDL break: 997 trades, win 51%, -0.39 R net, ₹-1,193/month, 2025 ₹-10,329, yrs up 0/10', 'ALL': 'ALL: 3346 trades, win 52%, -0.36 R net, ₹-3,961/month, 2025 ₹-61,124, yrs up 0/10'},
    ('MCX GOLD', 5): {'50%': '50%: 1155 trades, win 50%, -0.37 R net, ₹-1,675/month, 2025 ₹-23,768, yrs up 0/10', 'PDH break': 'PDH break: 1168 trades, win 55%, -0.35 R net, ₹-1,338/month, 2025 ₹-26,732, yrs up 0/10', 'PDL break': 'PDL break: 981 trades, win 55%, -0.32 R net, ₹-940/month, 2025 ₹-4,581, yrs up 0/10', 'ALL': 'ALL: 3304 trades, win 53%, -0.35 R net, ₹-3,954/month, 2025 ₹-55,082, yrs up 0/10'},
    ('MCX GOLD', 15): {'50%': '50%: 1128 trades, win 48%, -0.41 R net, ₹-1,860/month, 2025 ₹-20,189, yrs up 0/10', 'PDH break': 'PDH break: 1119 trades, win 52%, -0.39 R net, ₹-1,425/month, 2025 ₹-18,124, yrs up 0/10', 'PDL break': 'PDL break: 936 trades, win 49%, -0.39 R net, ₹-1,350/month, 2025 ₹-10,112, yrs up 0/10', 'ALL': 'ALL: 3183 trades, win 50%, -0.40 R net, ₹-4,637/month, 2025 ₹-48,425, yrs up 0/10'},
    ('MCX NATURALGAS', 3): {'50%': '50%: 1253 trades, win 49%, -0.12 R net, ₹-201/month, 2025 ₹-3,905, yrs up 1/10', 'PDH break': 'PDH break: 1123 trades, win 54%, -0.02 R net, ₹+39/month, 2025 ₹+6,384, yrs up 3/10', 'PDL break': 'PDL break: 1163 trades, win 58%, +0.06 R net, ₹+181/month, 2025 ₹+2,390, yrs up 9/10', 'ALL': 'ALL: 3539 trades, win 53%, -0.03 R net, ₹+19/month, 2025 ₹+4,868, yrs up 4/10'},
    ('MCX NATURALGAS', 5): {'50%': '50%: 1239 trades, win 50%, -0.09 R net, ₹-159/month, 2025 ₹-4,785, yrs up 2/10', 'PDH break': 'PDH break: 1099 trades, win 55%, +0.01 R net, ₹+76/month, 2025 ₹+3,460, yrs up 6/10', 'PDL break': 'PDL break: 1146 trades, win 59%, +0.09 R net, ₹+262/month, 2025 ₹+1,831, yrs up 8/10', 'ALL': 'ALL: 3484 trades, win 55%, -0.00 R net, ₹+179/month, 2025 ₹+506, yrs up 6/10'},
    ('MCX NATURALGAS', 15): {'50%': '50%: 1187 trades, win 49%, -0.11 R net, ₹-195/month, 2025 ₹-4,838, yrs up 2/10', 'PDH break': 'PDH break: 1072 trades, win 53%, -0.04 R net, ₹-33/month, 2025 ₹+3,909, yrs up 4/10', 'PDL break': 'PDL break: 1116 trades, win 57%, +0.05 R net, ₹+159/month, 2025 ₹+5,945, yrs up 9/10', 'ALL': 'ALL: 3375 trades, win 53%, -0.03 R net, ₹-69/month, 2025 ₹+5,015, yrs up 4/10'},
    ('NIFTY 50', 3): {'50%': '50%: 1186 trades, win 49%, -0.10 R net, ₹-1,683/month, 2025 ₹-29,382, yrs up 2/11', 'PDH break': 'PDH break: 1390 trades, win 50%, -0.09 R net, ₹-1,764/month, 2025 ₹-36,177, yrs up 3/11', 'PDL break': 'PDL break: 1143 trades, win 53%, -0.05 R net, ₹-765/month, 2025 ₹-3,792, yrs up 4/11', 'ALL': 'ALL: 3719 trades, win 51%, -0.08 R net, ₹-4,213/month, 2025 ₹-69,352, yrs up 1/11'},
    ('NIFTY 50', 5): {'50%': '50%: 1161 trades, win 49%, -0.12 R net, ₹-1,883/month, 2025 ₹-26,834, yrs up 3/11', 'PDH break': 'PDH break: 1374 trades, win 51%, -0.07 R net, ₹-1,300/month, 2025 ₹-9,842, yrs up 2/11', 'PDL break': 'PDL break: 1136 trades, win 52%, -0.06 R net, ₹-920/month, 2025 ₹-15,095, yrs up 6/11', 'ALL': 'ALL: 3671 trades, win 51%, -0.08 R net, ₹-4,104/month, 2025 ₹-51,772, yrs up 0/11'},
    ('NIFTY 50', 15): {'50%': '50%: 1112 trades, win 48%, -0.14 R net, ₹-2,082/month, 2025 ₹-18,064, yrs up 1/11', 'PDH break': 'PDH break: 1298 trades, win 49%, -0.11 R net, ₹-1,927/month, 2025 ₹+7,575, yrs up 2/11', 'PDL break': 'PDL break: 1068 trades, win 53%, -0.04 R net, ₹-593/month, 2025 ₹+5,694, yrs up 5/11', 'ALL': 'ALL: 3478 trades, win 50%, -0.10 R net, ₹-4,603/month, 2025 ₹-4,794, yrs up 1/11'},
}

def mid_trades(df5: pd.DataFrame, m1: pd.DataFrame | None, tgt=30.0, sl=30.0, mode="first", cost=COST,
               sess=(555, 930), last_entry=870, exit_tod=915, adr_k=None, breakouts=False) -> tuple:
    """(levels {day: (pdh, pdl, M, first_i, last_i)}, trades). Pure; m1 = 1-minute bars for exits (None → chart bars).
    sess = session minutes [start, end) used for the day's high / low; adr_k → target = stop = adr_k × ADR10 (average
    high − low of the previous 10 sessions) instead of fixed points (commodities).
    breakouts → also trade the first close ABOVE the previous day's high (CALL) and BELOW its low (PUT), once each a day.
    One trade at a time; a signal while a trade is open is ignored."""
    if df5.empty:
        return {}, []
    tod5 = df5.index.hour * 60 + df5.index.minute
    x5 = df5[(tod5 >= sess[0]) & (tod5 < sess[1])]
    day = x5.index.normalize()
    days = list(dict.fromkeys(day))
    D = x5.groupby(day).agg(h=("high", "max"), l=("low", "min"), c=("close", "last"))
    adr = (D.h - D.l).rolling(10).mean().shift(1)
    pos = pd.Series(np.arange(len(df5)), index=df5.index)
    src = m1 if m1 is not None and len(m1) else x5
    s_day = src.index.normalize()
    s_bounds = {d: (int(a), int(b)) for d, a, b in zip(*np.unique(s_day, return_index=True),
                                                       np.r_[np.unique(s_day, return_index=True)[1][1:], len(src)])}
    sT = src.index.values
    sO, sH, sL, sC = (src[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    sTod = (src.index.hour * 60 + src.index.minute).to_numpy()
    xO, xH, xL, xC = (x5[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    xT = x5.index
    xpos = pd.Series(np.arange(len(x5)), index=x5.index)
    levels, trades = {}, []
    max_mid = 1 if mode == "first" else 3
    for k in range(1, len(days)):
        d, p = days[k], days[k - 1]
        if (d - p).days > 5:
            continue
        pdh, pdl, pcl = float(D.at[p, "h"]), float(D.at[p, "l"]), float(D.at[p, "c"])
        M = pdl + (pdh - pdl) / 2
        ii = np.flatnonzero(day == d)
        is_last = d == days[-1]
        if len(ii) < 20 and not is_last:
            continue
        levels[d] = (pdh, pdl, M, int(pos[xT[ii[0]]]), int(pos[xT[ii[-1]]]))
        if adr_k is not None:
            a = adr.get(d, np.nan)
            if not np.isfinite(a) or a <= 0:
                continue
            tgt = sl = float(adr_k * a)
        sb = s_bounds.get(np.datetime64(d, "ns"))
        if sb is None:
            continue
        free_from, n_mid, up_done, dn_done = xT[ii[0]], 0, False, False
        for q in range(len(ii) - 1):
            i = ii[q]
            entry_t = xT[ii[q + 1]]
            if entry_t.hour * 60 + entry_t.minute > last_entry:
                break
            if entry_t < free_from:
                continue
            o, h, l, c = xO[i], xH[i], xL[i], xC[i]
            prev_c = xC[ii[q - 1]] if q > 0 else pcl
            side, kind = 0, None
            if n_mid < max_mid:
                side = 1 if (o > M and l <= M and c > M) else -1 if (o < M and h >= M and c < M) else 0
                kind = "50%" if side else None
            if not side and breakouts:
                if not up_done and c > pdh and prev_c <= pdh:
                    side, kind = 1, "PDH break"
                elif not dn_done and c < pdl and prev_c >= pdl:
                    side, kind = -1, "PDL break"
            if not side:
                continue
            j0 = sb[0] + int(np.searchsorted(sT[sb[0]:sb[1]], np.datetime64(entry_t)))
            if j0 >= sb[1]:
                break
            e = sO[j0]
            T, S = e + side * tgt, e - side * sl
            seg_ = slice(j0, sb[1])
            tod_s, H_s, L_s, O_s = sTod[seg_], sH[seg_], sL[seg_], sO[seg_]
            first = lambda m: int(np.argmax(m)) if m.any() else len(m)
            mx = first(tod_s >= exit_tod)
            ms = first(L_s <= S) if side > 0 else first(H_s >= S)
            mt = first(H_s >= T) if side > 0 else first(L_s <= T)
            tr = {"side": side, "kind": kind, "sig_t": xT[i], "entry_t": entry_t, "entry": float(e), "target": T,
                  "stop": S, "level": M if kind == "50%" else (pdh if side > 0 else pdl), "risk": sl}
            nn = len(tod_s)
            m = min(mx, ms, mt)
            if m < nn:
                if mx == m:
                    tr.update(exit_t=pd.Timestamp(sT[j0 + mx]), exit=float(O_s[mx]), why=f"{exit_tod // 60}:{exit_tod % 60:02d}")
                elif ms <= mt:
                    px = S if ms == 0 else (min(O_s[ms], S) if side > 0 else max(O_s[ms], S))
                    tr.update(exit_t=pd.Timestamp(sT[j0 + ms]), exit=float(px), why="stop")
                else:
                    tr.update(exit_t=pd.Timestamp(sT[j0 + mt]), exit=float(T), why="target")
            elif not is_last:
                tr.update(exit_t=pd.Timestamp(sT[sb[1] - 1]), exit=float(sC[sb[1] - 1]), why="day end")
            if "exit_t" in tr:
                tr["gross"] = side * (tr["exit"] - e)
                tr["net"] = tr["gross"] - cost * e
                free_from = tr["exit_t"] + pd.Timedelta(minutes=1)
            trades.append(tr)
            if kind == "50%":
                n_mid += 1
            elif kind == "PDH break":
                up_done = True
            else:
                dn_done = True
            if "exit_t" not in tr:
                break
    return levels, trades


def tally(trades, lot=LOT) -> str:
    done = [t for t in trades if "net" in t]
    if not done:
        return "no trades"
    net = np.array([t["net"] for t in done])
    g = np.array([t["gross"] for t in done])
    months = max(1, pd.DatetimeIndex([t["entry_t"] for t in done]).to_period("M").nunique())
    tg = sum(t["why"] == "target" for t in done)
    st = sum(t["why"] == "stop" for t in done)
    return (f"{len(done)} trades · target {tg} / stop {st} · win {round(100 * (net > 0).mean())}% · "
            f"gross {g.sum():+.0f} pts · net {net.sum():+.0f} pts · 1 lot ₹{net.sum() * lot:+,.0f} "
            f"(₹{net.sum() * lot / months:+,.0f}/month)")


class MidLevel(Indicator):
    id, name = "mid_level", "Prev-day 50% level (touch & go)"
    description = ("Previous day's (high + low) / 2. Wick touches it from above and closes above → CALL; from below and "
                   "closes below → PUT. NIFTY: target 30 / stop 30, out 15:15. MCX natural gas / crude / gold: target = "
                   "stop = k × ADR10, entries to 22:00, out 23:00. Backtests LOSE after costs.")
    warmup = 400
    params = [Param("target", "float", 30.0, "Index: target (points)", 5, 500, 5),
              Param("stop", "float", 30.0, "Index: stop (points)", 5, 500, 5),
              Param("adr_k", "float", 0.09, "Commodities: target = stop = k × ADR10", 0.03, 1.0, 0.01),
              Param("mode", "choice", "First signal of the day", "Signals", options=["First signal of the day", "Every signal (max 3/day)"]),
              Param("breakouts", "bool", True, "Also trade a close above prev high (CALL) / below prev low (PUT)"),
              Param("show_level", "bool", True, "Draw the 50 % level (and prev high / low)")]

    def compute(self, df, target, stop, adr_k, mode, breakouts, show_level):
        if df.empty or len(df) < 100:
            return Output()
        step = np.diff(df.index.values).astype("timedelta64[m]").astype(int)
        tfm = int(np.median(step[step > 0]))
        if tfm not in (3, 5, 15):
            return Output(info={"note": "Works on the 3-, 5- or 15-minute chart only."})
        sym = df.attrs.get("symbol")
        s = symbols.all_symbols().get(sym) if sym else None
        mcx = sym in MCX
        if s is not None and not s.nse_hours and not mcx:
            return Output(info={"note": "For NSE-hours indices (09:15–15:30) and MCX natural gas, crude and gold."})
        m1 = None
        if sym:
            try:
                m1 = store.bars(sym, "1")
                m1 = m1[(m1.index >= df.index[0]) & (m1.index < df.index[-1] + pd.Timedelta(minutes=tfm))]
            except Exception:
                m1 = None
        md = "first" if mode.startswith("First") else "all"
        if mcx:
            lot = s.lot
            levels, trades = mid_trades(df, m1, mode=md, cost=0.0003, sess=(540, 1440), last_entry=1320, exit_tod=1380,
                                        adr_k=float(adr_k), breakouts=bool(breakouts))
            rules = (f"level = (prev high + prev low) / 2 · wick touch + close back = CALL / PUT · close above PDH = CALL, below PDL = PUT · target = stop = "
                     f"{adr_k:g} × ADR10 · entries to 22:00 · 23:00 exit · cost 0.03 % · ₹ per mini lot ({lot})")
        else:
            lot = LOT
            levels, trades = mid_trades(df, m1, float(target), float(stop), md, breakouts=bool(breakouts))
            rules = (f"level = (prev high + prev low) / 2 · wick touch + close back = CALL (from above) / PUT (from below) "
                     f"· target {target:g} / stop {stop:g} · entries to 14:30 · 15:15 exit · cost 0.02 % · ₹ per lot ({lot})")
        t = times(df)
        tpos = lambda ts: int(t[max(0, int(df.index.searchsorted(pd.Timestamp(ts).floor(f"{tfm}min"), side="right")) - 1)])
        shapes, markers = [], []
        if show_level:
            for d, (pdh, pdl, M, a, b) in levels.items():
                shapes.append(seg(t[a], t[b], M, "#ab47bc", "50%", 2, 0, t[a]))
                shapes.append(seg(t[a], t[b], pdh, "#78909c", "PDH", 1, 2, t[a]))
                shapes.append(seg(t[a], t[b], pdl, "#78909c", "PDL", 1, 2, t[a]))
        dp = 2 if mcx and sym == "MCX NATURALGAS" else 1
        for tr in trades:
            up = tr["side"] > 0
            ts_sig = tpos(tr["sig_t"])
            markers.append({"time": ts_sig, "position": "belowBar" if up else "aboveBar",
                            "shape": "arrowUp" if up else "arrowDown", "color": "#26a69a" if up else "#ef5350",
                            "visible_from": ts_sig, "text": f"{'' if tr['kind'] == '50%' else tr['kind'] + ' '}{'CALL' if up else 'PUT'} @ {tr['entry']:.{dp}f}"})
            te = tpos(tr["entry_t"])
            end = tpos(tr["exit_t"]) if "exit_t" in tr else int(t[-1])
            shapes.append(seg(te, end, tr["target"], "#26a69a", "T", 1, 1, te))
            shapes.append(seg(te, end, tr["stop"], "#ef5350", "SL", 1, 1, te))
            if "net" in tr:
                ok = tr["net"] > 0
                markers.append({"time": end, "position": "aboveBar" if up else "belowBar", "shape": "square",
                                "color": "#26a69a" if ok else "#ef5350", "visible_from": end,
                                "text": f"{'✓' if ok else '✗'} {tr['why']} {tr['gross']:+.{dp}f} pts (₹{tr['net'] * lot:+,.0f} net)"})
        info = {"rules": rules,
                "on this chart": tally(trades, lot)}
        b = BT.get((sym if sym in MCX else "NIFTY 50", tfm), {})
        if sym not in (None, "NIFTY 50") and not mcx:
            info["backtest"] = "not backtested on this symbol (NIFTY numbers below)"
        for k in (("50%", "PDH break", "PDL break", "ALL") if breakouts else ("50%",)):
            if k in b:
                info[f"backtest {k}"] = b[k]
        if breakouts:
            for k in ("50%", "PDH break", "PDL break"):
                sub = [x for x in trades if x["kind"] == k]
                info[f"chart · {k}"] = tally(sub, lot)
        today = df.index[-1].normalize()
        if today in levels:
            pdh, pdl, M, *_ = levels[today]
            tt = [x for x in trades if x["entry_t"].normalize() == today]
            s_ = f"50% level {M:.{dp}f} (prev high {pdh:.{dp}f}, low {pdl:.{dp}f})"
            if tt:
                x = tt[-1]
                s_ += f" · {'CALL' if x['side'] > 0 else 'PUT'} {x['entry']:.{dp}f} → " + \
                      (f"{x['why']} {x['gross']:+.{dp}f} pts" if "net" in x else f"open (T {x['target']:.{dp}f} / SL {x['stop']:.{dp}f})")
            else:
                s_ += " · no signal yet"
            info["today"] = s_
        return Output(shapes=shapes, markers=markers, info=info)


INDICATORS = [MidLevel()]
