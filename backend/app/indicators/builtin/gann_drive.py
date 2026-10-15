"""Gann opening drive (NIFTY 5-minute) — a CANDIDATE under forward test, not a proven strategy.

Rules (indicators/tests/gann_trend.py, 2026-10-13):
  levels  : L(k) = (sqrt(09:15 open) + 0.25 k)^2 — Gann Square of 9, 45° steps (~0.33 % apart).
  drive   : the 09:15 bar's body |close − open| ≥ 0.25 × ADR10 (average range of the last 10 days) = strong opening drive.
  signal  : the first 5m bar starting 09:20–14:30 that closes above L(+1) (buy) or below L(−1) (sell). Taken only on
            strong-drive days; on other days it is shown grey as "skip".
  trade   : enter at the next bar's open, target 35 pts, stop 35 pts (checked on 1-minute bars when available, stop
            first if both fall in one minute), square off 15:15.
Backtest after 0.02 % costs: 2015–21 win 57 %, +2.9 pts/trade; 2022–Jun 2026 win 59 %, +2.2 pts; ~4 trades a month;
8 of 12 years positive (2024: −7.2). Found by checking ~10 clues, so it may be luck: the "live" tally counts only days
from 13 Oct 2026 on — judge it on that after 2–3 months. Gann's own far target with the same signal LOST since 2022.
"""
import numpy as np
import pandas as pd

from ... import store, symbols
from ..base import Indicator, Output, Param, times, to_epoch
from .levels import seg

LIVE_FROM = pd.Timestamp("2026-10-13")
# MCX crude (indicators/tests/gann_markets.py, 2026-10-13): same rule, session 09:00 → 23:00, target = stop = 0.15×ADR10,
# costs 0.03 %: ≤2021 win 58 %, +0.08 R · 2022–Oct26 win 58 %, +0.10 R, ~2–3 trades/month (79 trades) — gold, 10 big
# stocks and BTC lost with the same rule, so crude may be luck too.
# EMA 9/21 agreement filter (indicators/tests/ema_gann.py, 2026-10-14; 1 of 3 EMA×Gann variants tested):
EMA_BACKTEST = ("with EMA 9/21 filter: 2015–21 win 57%, +3.2 pts · 2022–Jun26 win 62%, +3.9 pts, ~2.5/month "
                "(removed trades +2.0 / −0.6) — weak evidence, forward test")
CRUDE = ("MCX CRUDEOIL",)
CRUDE_K = 0.15
CRUDE_LIVE_FROM = pd.Timestamp("2026-10-14")
CRUDE_BACKTEST = "MCX crude 2022–Oct26: ~2–3/month, win 58%, +0.10 R/trade (79 trades; ≤2021 +0.08 R) — thin, may be luck"


def day_trades(df5: pd.DataFrame, m1: pd.DataFrame | None, step=0.25, drive_k=0.25, tgt=35.0, stop=35.0,
               open_tod=555, sig_to=870, sq=915, cost=0.0002, adr_k=None):
    """one row per day with a Gann signal (strong drive or not); pure, used by the indicator and the tests.
    NSE defaults (09:15 open, signals to 14:30, 15:15 exit, fixed pts). Other sessions: open_tod=None (the day's first
    bar), sig_to / sq in minutes of the day, adr_k → target = stop = adr_k × ADR10."""
    if df5.empty:
        return []
    tod = (df5.index.hour * 60 + df5.index.minute).to_numpy()
    O, C = df5.open.to_numpy(), df5.close.to_numpy()
    day = df5.index.normalize()
    D = df5.groupby(day).agg(h=("high", "max"), l=("low", "min"))
    adr = (D.h - D.l).rolling(10).mean().shift(1)
    idx = pd.Series(np.arange(len(df5)), index=df5.index).groupby(day)
    m1t = m1.index.values if m1 is not None and len(m1) else None
    out = []
    for d in D.index:
        ix = idx.get_group(d).to_numpy()
        if (open_tod is not None and tod[ix[0]] != open_tod) or not np.isfinite(adr[d]) or adr[d] <= 0:
            continue
        if adr_k:
            tgt = stop = adr_k * adr[d]
        o = O[ix[0]]
        r = np.sqrt(o)
        lv = {k: (r + step * k) ** 2 for k in range(-3, 4)}
        body = abs(C[ix[0]] - o)
        row = dict(day=d, open=o, levels=lv, ix=ix, drive=body / adr[d], strong=body >= drive_k * adr[d])
        for j in ix:
            if j == ix[0] or tod[j] < tod[ix[0]] + 5:
                continue
            if tod[j] > sig_to:
                break
            dirn = 1 if C[j] > lv[1] else -1 if C[j] < lv[-1] else 0
            if not dirn:
                continue
            row.update(sig=j, dir=dirn)
            if j + 1 < len(C) and day[j + 1] == d:
                e = O[j + 1]
                row.update(entry_i=j + 1, entry=e)
                s_, t_ = e - dirn * stop, e + dirn * tgt
                close_t = (d + pd.Timedelta(minutes=sq)).to_datetime64()
                exit_ = None
                a = np.searchsorted(m1t, df5.index.values[j + 1]) if m1t is not None else 0
                if m1t is not None and a < len(m1t) and pd.Timestamp(m1t[a]).normalize() == d:   # this day has 1m
                    b = np.searchsorted(m1t, min(close_t, df5.index.values[ix[-1]] + np.timedelta64(5, "m")))
                    for m in range(a, b):
                        hi, lo, op = m1.high.iat[m], m1.low.iat[m], m1.open.iat[m]
                        if (lo <= s_) if dirn == 1 else (hi >= s_):          # a gap through the stop fills at the open
                            exit_ = (m1t[m], min(op, s_) if dirn == 1 else max(op, s_), "stop"); break
                        if (hi >= t_) if dirn == 1 else (lo <= t_):
                            exit_ = (m1t[m], t_, "target"); break
                    if exit_ is None and b < len(m1t) and m1t[b] >= close_t and pd.Timestamp(m1t[b]).normalize() == d:
                        exit_ = (m1t[b], m1.open.iat[b], "15:15")
                else:                                           # 5m only: stop first if both inside one bar
                    for m in ix[ix > j]:
                        if tod[m] >= sq:
                            exit_ = (df5.index.values[m], O[m], "15:15"); break
                        hi, lo = df5.high.iat[m], df5.low.iat[m]
                        if (lo <= s_) if dirn == 1 else (hi >= s_):
                            exit_ = (df5.index.values[m], min(O[m], s_) if dirn == 1 else max(O[m], s_), "stop"); break
                        if (hi >= t_) if dirn == 1 else (lo <= t_):
                            exit_ = (df5.index.values[m], t_, "target"); break
                if exit_ is not None:
                    pts = (exit_[1] - e) * dirn
                    row.update(exit_t=pd.Timestamp(exit_[0]), exit=exit_[1], why=exit_[2], pts=pts - cost * e, tgt=tgt)
            break
        out.append(row)
    return out


def tally(rows):
    done = [r for r in rows if r.get("strong") and "pts" in r]
    if not done:
        return "no trades yet"
    p = np.array([r["pts"] for r in done])
    return f"{len(p)} trades · win {round(100 * (p > 0).mean())}% · avg {p.mean():+.1f} pts · total {p.sum():+.0f} pts"


class GannDrive(Indicator):
    id, name = "gann_drive", "Gann opening drive (forward test)"
    description = ("Gann Square-of-9 levels from the 09:15 open; trades the first close beyond ±1 only on strong-"
                   "opening-drive days, 35/35 exit. A candidate under forward test since 13 Oct 2026 — not proven.")
    warmup = 2000                                         # ≥ 11 sessions of 5m bars (NIFTY 75/day, crude ~170/day)
    params = [Param("show_levels", "bool", True, "Draw the Gann levels"),
              Param("show_skips", "bool", True, "Show signals on weak-drive days (grey, not traded)"),
              Param("target", "float", 35.0, "Target (index pts)", 5, 200),
              Param("stop", "float", 35.0, "Stop (index pts)", 5, 200),
              Param("ema_filter", "bool", False, "EMA 9/21 filter (NIFTY): trade only if EMA9 vs EMA21 agrees")]

    def compute(self, df, show_levels, show_skips, target, stop, ema_filter=False):
        if df.empty or len(df) < 100:
            return Output()
        step = np.diff(df.index.values).astype("timedelta64[m]").astype(int)
        if int(np.median(step[step > 0])) != 5:
            return Output(info={"note": "Works on the 5-minute chart only (signals are 5-minute closes)."})
        sym = df.attrs.get("symbol")
        s = symbols.all_symbols().get(sym) if sym else None
        crude = sym in CRUDE
        if s is not None and not s.nse_hours and not crude:
            return Output(info={"note": "Tested on NIFTY (09:15–15:30) and MCX crude only — gold, stocks and BTC lost."})
        m1 = None
        if sym and not crude:                             # crude: 5m stop-first exits, as in its backtest
            try:
                m1 = store.bars(sym, "1")
                m1 = m1[(m1.index >= df.index[0]) & (m1.index < df.index[-1] + pd.Timedelta(minutes=5))]
            except Exception:
                m1 = None
        if crude:
            rows = day_trades(df, None, open_tod=None, sig_to=1320, sq=1380, cost=0.0003, adr_k=CRUDE_K)
        else:
            rows = day_trades(df, m1, tgt=target, stop=stop)
            if ema_filter:                                # EMA 9/21 on 5m closes must agree with the signal
                e9 = df.close.ewm(span=9, adjust=False).mean().to_numpy()
                e21 = df.close.ewm(span=21, adjust=False).mean().to_numpy()
                for r in rows:
                    if r.get("strong") and "sig" in r and (e9[r["sig"]] > e21[r["sig"]]) != (r["dir"] == 1):
                        r["strong"], r["ema_skip"] = False, True
        t = times(df)
        tpos = lambda ts: int(t[int(df.index.searchsorted(pd.Timestamp(ts).floor("5min"), side="right")) - 1])
        shapes, markers = [], []
        for r in rows:
            ix, lv = r["ix"], r["levels"]
            if show_levels:
                for k, price in lv.items():
                    col = "#ab47bc" if k == 0 else ("#26a69a" if k > 0 else "#ef5350")
                    shapes.append(seg(t[ix[0]], t[ix[-1]], price, col, f"G{k:+d}" if k else "G0", 1, 0 if k == 0 else 2, t[ix[0]]))
            if "sig" not in r or (not r["strong"] and not show_skips):
                continue
            j, dirn = r["sig"], r["dir"]
            lab = "BUY" if dirn == 1 else "SELL"
            if r["strong"]:
                markers.append({"time": int(t[j]), "position": "belowBar" if dirn == 1 else "aboveBar",
                                "shape": "arrowUp" if dirn == 1 else "arrowDown", "color": "#26a69a" if dirn == 1 else "#ef5350",
                                "visible_from": int(t[j]), "text": f"G {lab} · drive {r['drive']:.2f}×ADR"})
            else:
                markers.append({"time": int(t[j]), "position": "belowBar" if dirn == 1 else "aboveBar", "shape": "circle",
                                "color": "#78909c", "visible_from": int(t[j]),
                                "text": f"G {lab}? skip · {'EMA disagrees' if r.get('ema_skip') else 'drive %.2f×ADR' % r['drive']}"})
                continue
            if "entry_i" in r:
                ei = r["entry_i"]
                e = r["entry"]
                tg = r.get("tgt", target) if crude else target
                sl = tg if crude else stop
                shapes.append(seg(t[ei], t[ix[-1]], e + dirn * tg, "#26a69a", "T", 1, 1, t[ei]))
                shapes.append(seg(t[ei], t[ix[-1]], e - dirn * sl, "#ef5350", "SL", 1, 1, t[ei]))
            if "pts" in r:
                vis = int(to_epoch(pd.DatetimeIndex([r["exit_t"]]))[0])
                ok = r["pts"] > 0
                markers.append({"time": tpos(r["exit_t"]), "position": "aboveBar" if dirn == 1 else "belowBar", "shape": "square",
                                "color": "#26a69a" if ok else "#ef5350", "visible_from": max(vis, tpos(r["exit_t"])),
                                "text": f"{'✓' if ok else '✗'} {r['why']} {r['pts']:+.0f}"})
        if crude:
            info = {"rules": f"MCX crude: first 5m close beyond G±1 on strong-drive days (first bar body ≥ 0.25×ADR10) · "
                             f"target = stop = {CRUDE_K:g}×ADR10 (5m bars, stop first) · signals to 22:00 · 23:00 exit",
                    "backtest": CRUDE_BACKTEST,
                    "on this chart": tally(rows),
                    "live since 14 Oct 2026": tally([r for r in rows if r["day"] >= CRUDE_LIVE_FROM])}
        else:
            info = {"rules": f"buy/sell = first 5m close beyond G±1 on strong-drive days (09:15 body ≥ 0.25×ADR10) · "
                             f"target {target:g} / stop {stop:g} · 15:15 exit",
                    "backtest": (EMA_BACKTEST if ema_filter else
                                 "2022–Jun26: win 59%, +2.2 pts/trade, ~4/month (found among ~10 clues — may be luck)"),
                    "on this chart": tally(rows),
                    "live since 13 Oct 2026": tally([r for r in rows if r["day"] >= LIVE_FROM])}
        last = rows[-1] if rows else None
        if last is not None and last["day"] == df.index[-1].normalize():
            st = ("EMA 9/21 disagrees → no trade today" if last.get("ema_skip") else
                  "strong drive" if last["strong"] else "weak drive → no trade today")
            if "sig" not in last:
                st += " · no signal yet" if last["strong"] else ""
            elif not last["strong"]:
                pass                                       # skip day: no hypothetical result in the status
            elif "pts" in last:
                st += f" · {('BUY' if last['dir'] == 1 else 'SELL')} {last['why']} {last['pts']:+.1f} pts"
            elif "entry" in last:
                st += f" · {('BUY' if last['dir'] == 1 else 'SELL')} open from {last['entry']:.1f}"
            info["today"] = f"open {last['open']:.1f} · drive {last['drive']:.2f}×ADR · {st}"
        return Output(shapes=shapes, markers=markers, info=info)


INDICATORS = [GannDrive()]
