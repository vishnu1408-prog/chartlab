"""Strategy bots that paper-trade on real NIFTY option prices (each in its own paper account, see paper.py):
  bot   — Morning Edge (09:45 decision, E2 exit) — driven from edge.loop → paper.bot_morning_edge
  gann  — Gann opening drive: strong 09:15 body (≥ 0.25 × ADR10), first 5-minute close beyond (√open ± 0.25)²
          → buy ITM-200 CE / PE at the next bar's open; stop and target 35 NIFTY points (all lots); out at 15:15
  ng    — MCX natural gas opening drive (indicators/builtin/ng_drive.py; validator WEAK → forward test): strong first
          5m bar at 09:00 → buy the CE / PE one strike in the money (nearest expiry not expiring today), 1 lot;
          stop / target = futures ∓/± 0.15 × ADR10; out 23:00
  plan  — Daily Plan, at most one trade a day: the Gann drive if it enters before 09:45; else Morning Edge at 09:45
          (|pred| ≥ 0.10, E2 exit); else a later Gann signal (until 14:30).
Signals use the same functions as the backtests (indicators/builtin/gann_drive.day_trades, the saved Morning Edge
decision). A signal noticed more than LATE_MIN minutes after its entry time is skipped (PC off / no data) and logged,
so the live record only holds trades taken at the price the backtest assumes.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

import pandas as pd

from . import config

log = logging.getLogger("chartlab.bots")
SYM = "NIFTY 50"
LATE_MIN = 10
ME_TRIGGER = 0.10
status: dict = {"state": "idle", "gann": None, "error": None}
_tried: set = set()                                          # (account, day, kind) attempted today (no retry loops)


def _closed_today(df5: pd.DataFrame, now: dt.datetime) -> pd.DataFrame | None:
    closed = df5[(df5.index + pd.Timedelta(minutes=5)) <= pd.Timestamp(now)]
    return None if closed.empty or closed.index[-1].normalize() != pd.Timestamp(now.date()) else closed


def gann_today(df5: pd.DataFrame, now: dt.datetime) -> dict | None:
    """Today's Gann row (drive, strong, sig …) from CLOSED 5-minute bars, None before the 09:15 bar closes. Pure."""
    from .indicators.builtin.gann_drive import day_trades
    closed = None if df5.empty else _closed_today(df5, now)
    if closed is None:
        return None
    r = next((x for x in day_trades(closed, None) if x["day"] == pd.Timestamp(now.date())), None)
    return r and {**r, "closed": closed}


def gann_signal(df5: pd.DataFrame, now: dt.datetime) -> dict | None:
    """Today's Gann drive signal from 5-minute bars (history + today's CLOSED bars). Pure.
    Returns {"dir", "sig_t", "entry_t", "drive", "level"} or None."""
    r = gann_today(df5, now)
    if not r or not r.get("strong") or "sig" not in r:
        return None
    sig_t = r["closed"].index[r["sig"]]
    return {"dir": int(r["dir"]), "sig_t": sig_t, "entry_t": sig_t + pd.Timedelta(minutes=5),
            "drive": round(float(r["drive"]), 2), "level": round(float(r["levels"][1 if r["dir"] > 0 else -1]), 2)}


def plan_choice(now: dt.datetime, g: dict | None, me: dict | None) -> tuple[str | None, str]:
    """Daily Plan: what to trade now (None = nothing yet). Pure. me = today's Morning Edge decision or None."""
    t945 = pd.Timestamp(now.date()) + pd.Timedelta(minutes=585)
    if g and g["entry_t"] < t945:
        return "gann", "Gann drive entered before 09:45"
    if pd.Timestamp(now) < t945:
        return None, "waiting for 09:45"
    if me and abs(float(me.get("pred") or 0)) >= ME_TRIGGER:
        return "me", f"Morning Edge pred {me['pred']:+.3f}"
    if me is None and pd.Timestamp(now) < t945 + pd.Timedelta(minutes=8):
        return None, "waiting for the Morning Edge decision"
    if g:
        return "gann", "later Gann signal (Morning Edge had no trade)"
    return None, "no signal yet"


_open_try: dict = {}


def _today_minutes(sym: str, open_min: int, now: dt.datetime) -> pd.DataFrame | None:
    """Today's 1-minute bars from the live feed; if the session's FIRST minute is missing (Angel refused the candle
    request when the feed started), fetch the morning candles directly (at most once a minute) and save them."""
    from . import sources, store, sync
    m1 = sync.latest_minutes(sym)
    day0 = pd.Timestamp(now.date()) + pd.Timedelta(minutes=open_min)
    have_open = m1 is not None and len(m1) and m1.index[0] <= day0
    if not have_open and time.time() - _open_try.get(sym, 0) > 60:
        _open_try[sym] = time.time()
        try:
            c = sources.Angel.candles(sym, day0.to_pydatetime(), now, attempts=2)
            if len(c):
                store.save_ext(sym, c, "1")
                m1 = c if m1 is None or not len(m1) else pd.concat([c[c.index < m1.index[0]], m1]).sort_index()
        except Exception as e:
            log.warning("bots: morning candles %s: %s", sym, e)
    return m1


def _bars(now: dt.datetime) -> pd.DataFrame:
    from . import store
    hist = store.bars(SYM, "5")
    hist = hist[(hist.index >= pd.Timestamp(now.date()) - pd.Timedelta(days=25)) & (hist.index < pd.Timestamp(now.date()))]
    m1 = _today_minutes(SYM, 555, now)
    if m1 is None or not len(m1):
        return hist
    today = store.resample(m1[m1.index >= pd.Timestamp(now.date())], "5")
    return pd.concat([hist, today[["open", "high", "low", "close"]]]).sort_index()


def _done_today(st: dict, prefix: str) -> bool:
    return any((x.get("note") or "").startswith(prefix) for x in st["positions"] + st["trades"])


def _noted(st: dict, prefix: str) -> bool:
    """traded, skipped or 'no trade' already logged today (survives restarts)"""
    return _done_today(st, prefix) or any((e.get("text") or "").startswith(prefix) for e in st["events"])


def no_trade_reason(name: str, r: dict | None, m: int, last: int) -> str | None:
    """Why a drive bot has no trade today (None = it may still come). r = today's drive row; m / last = minutes
    of day now / of the last possible signal. Pure."""
    if r is None:
        return None
    if not r.get("strong"):
        return f"opening drive {float(r['drive']):.2f}×ADR (needs 0.25) — no trade today"
    if "sig" not in r and m > last:
        return f"strong drive {float(r['drive']):.2f}×ADR but no 5-minute close beyond the level — no trade today"
    return None


def note_no_trade(account: str, prefix: str, why: str, now: dt.datetime):
    """log once a day that the bot looked and found nothing, so an empty account shows it is alive"""
    from . import paper
    with paper._lock:
        st = paper.load(account)
        if _noted(st, prefix):
            return
        paper._event(st, "NO TRADE", f"{prefix}: {why}", now)
        paper.save(st)


def trade(account: str, kind: str, dirn: int, now: dt.datetime, note: str) -> dict | None:
    """Buy the ITM-200 option for `kind` ("gann": 35/35 all lots; "me": E2 — half at +35, rest to entry)."""
    from . import paper
    with paper._lock:
        st = paper.load(account)
        _, under = paper.quotes([], (SYM,))
        spot = under.get(SYM)
        if not spot:
            paper._event(st, "REJECTED", f"{note}: no live NIFTY price", now)
            paper.save(st)
            return None
        exps = sorted({r[0] for r in paper.contracts(SYM)[2] if r[0] >= now.date()})
        K, typ = paper.itm200(spot, dirn)
        c = paper.find(SYM, exps[0].isoformat(), K, typ) if exps else None
        if c is None:
            paper._event(st, "REJECTED", f"{note}: no ITM-200 contract", now)
            paper.save(st)
            return None
        q, under = paper.quotes([c], (SYM,))
        u = under.get(SYM, spot)
        lots = int(st["settings"].get("bot_lots", paper.BOT_LOTS))
        plan = {"stop": {"basis": "index", "price": round(u - 35 * dirn, 2)}, "note": note}
        if kind == "me":
            plan.update(target={"basis": "index", "price": round(u + 35 * dirn, 2), "lots": max(1, lots // 2)},
                        be_after_target=True, **paper.target2(st))
        else:
            plan.update(target={"basis": "index", "price": round(u + 35 * dirn, 2)}, be_after_target=False)
        rec = paper.place(st, {"contract": c, "side": "BUY", "lots": lots, "order_type": "MARKET",
                               "source": f"bot:{account}", **plan}, q, under, now)
        paper.save(st)
        log.info("bot %s %s: %s", account, note, rec.get("status"))
        return rec


NG_SYM = "MCX NATURALGAS"


def ng_today(df5: pd.DataFrame, now: dt.datetime) -> dict | None:
    """Today's natural-gas first-bar row from CLOSED 5m bars (None before the first bar closes). Pure."""
    from .indicators.builtin.ng_drive import ng_trades
    closed = None if df5.empty else _closed_today(df5, now)
    if closed is None:
        return None
    r = next((x for x in ng_trades(closed, 0.0) if x["day"] == pd.Timestamp(now.date())), None)
    return r and {**r, "closed": closed}


def ng_signal(df5: pd.DataFrame, now: dt.datetime) -> dict | None:
    """Today's natural-gas opening-drive signal from CLOSED 5m bars (history + today). Pure."""
    from .indicators.builtin.ng_drive import ng_trades
    r = ng_today(df5, now)
    if not r or not r.get("strong") or "sig" not in r:
        return None
    sig_t = r["closed"].index[r["sig"]]
    return {"dir": int(r["dir"]), "sig_t": sig_t, "entry_t": sig_t + pd.Timedelta(minutes=5),
            "risk": float(0.15 * r["adr"]), "drive": round(float(r["drive"]), 2)}


def ng_trade(now: dt.datetime, g: dict, note: str) -> dict | None:
    from . import paper
    with paper._lock:
        st = paper.load("ng")
        seg, name, rows = paper.contracts(NG_SYM)
        exps = sorted({r[0] for r in rows if r[0] > now.date()})
        if not exps:
            paper._event(st, "REJECTED", f"{note}: no natural-gas option expiry", now)
            paper.save(st)
            return None
        exp = exps[0]
        probe = next(r for r in rows if r[0] == exp)
        q0, under = paper.quotes([{"symbol": NG_SYM, "seg": "MCX", "token": str(probe[3]), "expiry": exp.isoformat()}])
        u = under.get(NG_SYM)
        if not u:
            paper._event(st, "REJECTED", f"{note}: no live natural-gas futures price", now)
            paper.save(st)
            return None
        ks = sorted({r[1] for r in rows if r[0] == exp})
        atm = min(ks, key=lambda k: abs(k - u))
        i = ks.index(atm)
        K = ks[max(0, i - 1)] if g["dir"] > 0 else ks[min(len(ks) - 1, i + 1)]      # one strike in the money
        c = paper.find(NG_SYM, exp.isoformat(), K, "CE" if g["dir"] > 0 else "PE")
        if c is None:
            paper._event(st, "REJECTED", f"{note}: contract not found", now)
            paper.save(st)
            return None
        q, under = paper.quotes([c])
        u = under.get(NG_SYM, u)
        d, risk = g["dir"], g["risk"]
        rec = paper.place(st, {"contract": c, "side": "BUY", "lots": int(st["settings"].get("bot_lots", 1) or 1),
                               "order_type": "MARKET", "source": "bot:ng", "note": note,
                               "stop": {"basis": "index", "price": round(u - d * risk, 2)},
                               "target": {"basis": "index", "price": round(u + d * risk, 2)}, "be_after_target": False},
                    q, under, now)
        paper.save(st)
        log.info("bot ng %s: %s", note, rec.get("status"))
        return rec


def ng_tick(now: dt.datetime):
    from . import paper, store
    day = now.date().isoformat()
    hist = store.bars(NG_SYM, "5")
    hist = hist[(hist.index >= pd.Timestamp(now.date()) - pd.Timedelta(days=25)) & (hist.index < pd.Timestamp(now.date()))]
    m1 = _today_minutes(NG_SYM, 540, now)
    if m1 is None or not len(m1):
        return
    today = store.resample(m1[m1.index >= pd.Timestamp(now.date())], "5")
    bars = pd.concat([hist, today[["open", "high", "low", "close"]]]).sort_index()
    g = ng_signal(bars, now)
    status["ng"] = None if g is None else {k: str(v) for k, v in g.items()}
    st = paper.load("ng")
    if g is None and not _noted(st, f"NG {day}"):
        why = no_trade_reason("ng", ng_today(bars, now), now.hour * 60 + now.minute, 9 * 60 + 40)
        if why:
            note_no_trade("ng", f"NG {day}", why, now)
        return
    if not g or ("ng", day, "ng") in _tried or _done_today(st, f"NG {day}"):
        return
    _tried.add(("ng", day, "ng"))
    side = "BUY (CE)" if g["dir"] > 0 else "SELL (PE)"
    if pd.Timestamp(now) - g["entry_t"] > pd.Timedelta(minutes=LATE_MIN):
        paper._event(st, "SKIPPED", f"NG {day}: signal {side} at {g['sig_t']:%H:%M} noticed too late ({now:%H:%M})", now)
        paper.save(st)
        return
    ng_trade(now, g, f"NG {day} {side} · drive {g['drive']}×ADR · {'Monday' if now.weekday() == 0 else 'Tue–Fri'}")


def tick(now: dt.datetime | None = None):
    from . import db, paper
    from .sync import now_ist
    now = now or now_ist()
    day = now.date().isoformat()
    m = now.hour * 60 + now.minute
    if now.weekday() < 5 and 9 * 60 + 5 <= m <= 9 * 60 + 45:
        try:
            ng_tick(now)
        except Exception as e:
            log.exception("ng bot")
            status["ng_error"] = str(e)[:200]
    if now.weekday() >= 5 or not (9 * 60 + 20 <= m <= 14 * 60 + 40):
        return
    bars = _bars(now)
    g = gann_signal(bars, now)
    status["gann"] = None if g is None else {k: str(v) for k, v in g.items()}
    late = g is not None and pd.Timestamp(now) - g["entry_t"] > pd.Timedelta(minutes=LATE_MIN)
    side = lambda d: "BUY (CE)" if d > 0 else "SELL (PE)"
    # Gann bot
    gs = paper.load("gann")
    key = ("gann", day, "gann")
    if g is None and not _noted(gs, f"Gann {day}"):
        why = no_trade_reason("gann", gann_today(bars, now), m, 14 * 60 + 30)
        if why:
            note_no_trade("gann", f"Gann {day}", why, now)
    if g and key not in _tried and not _done_today(gs, f"Gann {day}"):
        _tried.add(key)
        if late:
            paper._event(gs, "SKIPPED", f"Gann {day}: signal {side(g['dir'])} at {g['sig_t']:%H:%M} noticed too late "
                                        f"({now:%H:%M}) — not traded", now)
            paper.save(gs)
        else:
            trade("gann", "gann", g["dir"], now, f"Gann {day} {side(g['dir'])} · drive {g['drive']}×ADR · close beyond "
                                                 f"{g['level']} at {g['sig_t']:%H:%M}")
    # Daily Plan bot
    ps = paper.load("plan")
    if _noted(ps, f"Daily Plan {day}") or ("plan", day, "done") in _tried:
        return
    me = db.edge_get(day)
    pick, why = plan_choice(now, g, me)
    if pick is None:
        if m > 14 * 60 + 30:
            mtxt = "no decision" if me is None else f"pred {float(me.get('pred') or 0):+.3f}, needs ±{ME_TRIGGER}"
            note_no_trade("plan", f"Daily Plan {day}", f"no Gann signal and Morning Edge {mtxt} — no trade today", now)
        return
    _tried.add(("plan", day, "done"))
    if pick == "gann":
        if late:
            paper._event(ps, "SKIPPED", f"Daily Plan {day}: Gann signal noticed too late ({now:%H:%M}) — not traded", now)
            paper.save(ps)
            return
        trade("plan", "gann", g["dir"], now, f"Daily Plan {day} · Gann {side(g['dir'])} · {why}")
    else:
        t_me = dt.datetime.fromisoformat(me["at"]) if me.get("at") else now
        if now - t_me > dt.timedelta(minutes=LATE_MIN):
            paper._event(ps, "SKIPPED", f"Daily Plan {day}: Morning Edge decision noticed too late — not traded", now)
            paper.save(ps)
            return
        d = 1 if me["pred"] > 0 else -1
        trade("plan", "me", d, now, f"Daily Plan {day} · Morning Edge {side(d)} · {why}")


def loop():
    from .sync import now_ist
    while True:
        try:
            t = now_ist()
            m = t.hour * 60 + t.minute
            if t.weekday() < 5 and 9 * 60 + 5 <= m <= 14 * 60 + 40:
                status["state"] = "watching"
                tick(t)
                status["error"] = None
                time.sleep(15)
                continue
            status["state"] = "idle"
            if m < 9 * 60:
                _tried.clear()
        except Exception as e:
            log.exception("bots")
            status["error"] = str(e)[:200]
        time.sleep(30)


def start():
    if config.OFFLINE:
        return
    threading.Thread(target=loop, daemon=True, name="bots").start()
