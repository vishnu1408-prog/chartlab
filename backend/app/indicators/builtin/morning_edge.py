"""Morning Edge (ML, forward test) — the NIFTY 09:45 model from app/edge.py drawn on the chart.

At 09:45 the model predicts NIFTY's 09:45 → 15:15 move in ADR units from 52 inputs (14 classic strategy signals,
price context, ~115-stock breadth, yesterday's option OI / dealer gamma / FII-Pro-Client futures positions, VIX).
|prediction| ≥ trigger (default 0.10, choose 0.05–0.20) → BUY (▲) or SELL (▼) at 09:45: stop 35 index pts, half booked at +35 then the rest's stop moves
to entry, 15:15 square-off. Below 0.15 → "skip" (grey).
History on the chart is honest: each year's signals come from a model trained only on earlier years (walk-forward);
2026 history from a model trained on 2015–2025 only; from 14 Oct 2026 the live 09:45 decisions (final model, trained to 12 Oct 2026).
Backtest with REAL ITM-200 option premiums, 2022 → Jun 2026: 343 trades, win 51 %, +2.2 option pts / trade,
PF 1.15 (≈ ₹900 / month / lot) — small and unproven live. Paper-trade it first.
"""
import numpy as np
import pandas as pd

from ... import edge, store, symbols
from ..base import Indicator, Output, Param, times, to_epoch
from .levels import seg


# real ITM-200 premiums, 2022 → Jun 2026, exit E2 (indicators/tests/book_options.py, model retrained 2026-10-13;
# debit spreads ATM/+100 and ITM-100/OTM+100 LOST on the same signals: −1.6 to −3.1 opt pts/trade)
BACKTEST = {"0.05": "earlier model, real ITM 2022–Jun26: 14/month, win 49%, +0.7 opt pts, ≈₹675/month/lot",
            "0.10": "real ITM-200 2022–Jun26: 8.9/month, win 49%, +1.9 opt pts, ≈₹1,090/month/lot, worst DD ₹25k",
            "0.15": "real ITM-200 2022–Jun26: 5.1/month, win 52%, +3.0 opt pts, ≈₹1,000/month/lot, worst DD ₹24k",
            "0.20": "earlier model, real ITM 2022–Jun26: ~4/month, win 53%, +2.8 opt pts (fewer trades)"}


def _epoch(ts) -> int:
    return int(to_epoch(pd.DatetimeIndex([pd.Timestamp(ts)]))[0])


def tally(rows) -> str:
    done = [r for r in rows if r.get("pts") is not None and r.get("done")]
    if not done:
        return "no trades yet"
    p = np.array([r["pts"] for r in done]) - 0.0002 * np.array([r["entry"] for r in done])
    return f"{len(p)} trades · win {round(100 * (p > 0).mean())}% · avg {p.mean():+.1f} NIFTY pts · total {p.sum():+.0f}"


class MorningEdge(Indicator):
    id, name = "morning_edge", "Morning Edge (ML, forward test)"
    description = ("09:45 machine-learning signal for NIFTY built from 14 classic strategies + breadth + option OI + "
                   "FII/Pro positions + VIX. Stop 35, half at +35, rest to breakeven, 15:15 exit. Forward test.")
    params = [Param("trigger", "choice", "0.10", "Trigger |pred| ≥ (ADR)", options=["0.05", "0.10", "0.15", "0.20"]),
              Param("show_skips", "bool", False, "Show no-trade days (grey)"),
              Param("show_lines", "bool", True, "Draw stop / +35 lines")]

    def compute(self, df, trigger, show_skips, show_lines):
        th = float(trigger)
        if df.empty:
            return Output()
        sym = df.attrs.get("symbol")
        if sym and sym != edge.SYM:
            return Output(info={"note": "Built for NIFTY 50 only."})
        step = np.diff(df.index.values).astype("timedelta64[m]").astype(int)
        tf = int(np.median(step[step > 0])) if len(step) else 5
        if tf > 15:
            return Output(info={"note": "Use an intraday chart (1–15 minutes): the signal is at 09:45."})
        sig = edge.signals()
        if sig.empty:
            return Output(info={"note": "No model yet — it trains with: docker exec chartlab python -m app.edge train"})
        days = df.index.normalize().unique()
        sig = sig[sig.index.isin(days)]
        m1 = None
        try:
            m1 = store.bars(edge.SYM, "1")
            m1 = m1[(m1.index >= df.index[0]) & (m1.index < df.index[-1] + pd.Timedelta(minutes=tf))]
        except Exception:
            m1 = None
        t = times(df)
        bar_at = lambda ts: int(t[max(0, int(df.index.searchsorted(pd.Timestamp(ts), side="right")) - 1)])
        markers, shapes, rows = [], [], []
        for d, r in sig.iterrows():
            pred = float(r.pred)
            if not np.isfinite(pred):
                continue
            t945 = d + pd.Timedelta(minutes=585)
            if t945 > df.index[-1] + pd.Timedelta(minutes=tf) or t945 < df.index[0]:
                continue
            src = {"live": "live", "final": "model ≤Jun26", "walk-forward": "walk-fwd"}.get(r.source, r.source)
            dirn = 1 if pred >= th else -1 if pred <= -th else 0
            x0 = bar_at(t945)
            if not dirn:
                if show_skips:
                    markers.append({"time": x0, "position": "aboveBar", "shape": "circle", "color": "#78909c",
                                    "visible_from": _epoch(t945), "text": f"ME skip {pred:+.2f}"})
                continue
            strong = abs(pred) >= edge.STRONG
            markers.append({"time": x0, "position": "belowBar" if dirn == 1 else "aboveBar",
                            "shape": "arrowUp" if dirn == 1 else "arrowDown", "color": "#26a69a" if dirn == 1 else "#ef5350",
                            "visible_from": _epoch(t945),
                            "text": f"ME {'BUY' if dirn == 1 else 'SELL'}{'!' if strong else ''} {pred:+.2f} ({src})"})
            if m1 is None:
                continue
            day = m1[(m1.index >= t945) & (m1.index <= d + pd.Timedelta(minutes=915))]
            if day.empty or day.index[0] != t945:
                continue
            res = edge.e2_legs(day, dirn)
            rows.append({"day": d, "src": r.source, "entry": res["entry"], "pts": res.get("pts"), "done": res["done"]})
            if show_lines:
                end = bar_at(min(day.index[-1], d + pd.Timedelta(minutes=915)))
                shapes.append(seg(x0, end, res["entry"] - dirn * edge.STOP, "#ef5350", "SL", 1, 1, x0))
                shapes.append(seg(x0, end, res["entry"] + dirn * edge.BOOK, "#26a69a", "+35", 1, 1, x0))
            for frac, when, pts, why in res["legs"]:
                markers.append({"time": bar_at(when), "position": "aboveBar" if dirn == 1 else "belowBar", "shape": "square",
                                "color": "#26a69a" if pts > 0 else ("#ef5350" if pts < 0 else "#78909c"),
                                "visible_from": max(_epoch(when), bar_at(when)),
                                "text": f"{'✓' if pts > 0 else '✗' if pts < 0 else '•'} {why} {pts:+.0f}{' (½)' if frac < 1 else ''}"})
        info = {"rules": f"09:45 · |pred| ≥ {th:.2f} ADR → BUY/SELL ITM · stop 35 · ½ at +35 then breakeven · 15:15 out",
                "backtest": BACKTEST.get(trigger, ""),
                "on this chart": tally(rows),
                "live since 14 Oct 2026": tally([r for r in rows if r["src"] == "live"])}
        today = df.index[-1].normalize()
        if today in sig.index and np.isfinite(sig.pred.get(today, np.nan)):
            p_ = float(sig.pred[today])
            info["today"] = (f"{'BUY' if p_ >= th else 'SELL' if p_ <= -th else 'no trade'} "
                             f"(pred {p_:+.2f} ADR, {sig.source[today]})")
        elif df.index[-1] >= today + pd.Timedelta(minutes=555):
            info["today"] = "decision at 09:45" if df.index[-1] < today + pd.Timedelta(minutes=585) else "no signal saved for today"
        return Output(shapes=shapes, markers=markers, info=info)


INDICATORS = [MorningEdge()]
