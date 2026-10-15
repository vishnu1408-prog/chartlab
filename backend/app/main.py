"""ChartLab API + static frontend.  Run: uvicorn app.main:app --port 8050  (from backend/)."""
import asyncio
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

import numpy as np
import pandas as pd
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

import datetime as dt
from pathlib import Path

from . import backtest, chain, config, edge, harmonic_scan, news, options, sources, store, symbols, sync
from .indicators import registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("chartlab")
@asynccontextmanager
async def lifespan(_app):
    news.connect().close()
    imp = Path(config.DATA_DIR) / "news_import.csv"              # headlines saved by the research logger
    if imp.exists():
        news.import_csv(imp)
    if not config.OFFLINE:
        sync.sync_async("NIFTY 50")              # refresh recent data in the background at start-up
        if sources.Angel.available():            # resume the MCX history downloads (they skip what is already done)
            for m in symbols.MCX:
                sync.backfill_async(m)
        threading.Thread(target=news.rss_loop, daemon=True).start()
        edge.start()                             # Morning Edge: NSE EOD files + the 09:45 decision
    yield


app = FastAPI(title="ChartLab", version="1.0", lifespan=lifespan)


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    resp = await call_next(request)
    if not request.url.path.startswith("/api") and not request.url.path.startswith("/vendor"):
        resp.headers["Cache-Control"] = "no-cache"          # always revalidate the app's own JS/CSS after an update
    return resp


def _sym(symbol: str):
    try:
        return symbols.get(symbol)
    except KeyError as e:
        raise HTTPException(404, str(e))


def _tf(tf: str):
    if tf not in store.TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {list(store.TIMEFRAMES)}")
    return tf


# ───────────────────────────── market data ─────────────────────────────
@app.get("/api/health")
def health(symbol: str | None = None):
    s = symbols.all_symbols().get(symbol) if symbol else None
    st = {"ok": True, **sources.source_status(), "market_open": sync.market_open(sym=symbol if s else None)}
    if s is not None and not s.nse_hours and s.exchange != "MCX":
        st.update(source="binance" if s.binance else "yahoo", detail=s.note)
    if s is not None and s.exchange == "MCX" and sync.backfill_status.get(symbol, "history complete") != "history complete":
        st["detail"] = f"{st['detail']} · {sync.backfill_status[symbol]}"
    return st


@app.get("/api/symbols")
def list_symbols():
    return [s.public() for s in symbols.all_symbols().values()]


@app.get("/api/coverage")
def coverage(symbol: str):
    _sym(symbol)
    return store.coverage(symbol)


@app.post("/api/sync")
def do_sync(symbol: str, force: bool = False):
    _sym(symbol)
    return sync.sync_symbol(symbol, force=force)


@app.get("/api/candles")
def candles(symbol: str, tf: str = "5", limit: int = Query(1500, ge=1, le=20000),
            before: int | None = None, start: int | None = None, end: int | None = None):
    _sym(symbol), _tf(tf)
    if before is None and start is None:
        sync.sync_async(symbol)                        # keep recent data fresh in the background (no-op if fresh)
    df = store.bars(symbol, tf)
    w = store.window(df, start=start, end=end, limit=limit, before=before)
    has_more = len(w) > 0 and len(df) > 0 and w.index[0] > df.index[0]
    return {"symbol": symbol, "tf": tf, "bars": store.records(w), "has_more": bool(has_more),
            "first": int(store.to_epoch(df.index[:1])[0]) if len(df) else None,
            "last": int(store.to_epoch(df.index[-1:])[0]) if len(df) else None}


@app.get("/api/chain")
def option_chain(symbol: str, expiry: str | None = None, n: int = 10):
    """Live option chain (Angel One quotes): strikes around the money, OI / volume in lots, IV estimate."""
    _sym(symbol)
    if not chain.supported(symbol):
        raise HTTPException(404, f"no option chain for {symbol} (NIFTY, BANKNIFTY, FINNIFTY, SENSEX, MCX gold / silver / crude / natural gas)")
    return chain.chain(symbol, expiry, n)


@app.get("/api/option")
def option_contract(symbol: str, time: int, type: str = "CE", spot: float | None = None, strike: float | None = None):
    """ATM option premium bars (real dataset prices when available, else Black-Scholes) for practice trading."""
    _sym(symbol)
    if type.upper() not in ("CE", "PE"):
        raise HTTPException(400, "type must be CE or PE")
    try:
        return options.contract(symbol, time, type, spot, strike)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ───────────────────────────── news ─────────────────────────────
@app.get("/api/news")
def get_news(end: int | None = None, start: int | None = None, q: str | None = None, limit: int = Query(150, ge=1, le=2000)):
    """Headlines up to `end` (replay: the replay clock — nothing from the future), newest first."""
    return news.query(end=end, start=start, q=q, limit=limit)


@app.post("/api/news/ensure")
def ensure_news(start: str, end: str, priority: bool = False):
    """Queue GDELT downloads for the weekdays in [start, end] (YYYY-MM-DD) that are not cached yet."""
    try:
        a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        raise HTTPException(400, "dates must be YYYY-MM-DD")
    if (b - a).days > 3660:
        raise HTTPException(400, "range too long (max 10 years)")
    return {"queued": news.ensure_days(a, b, priority=priority), **news.queue_info()}


@app.get("/api/news/status")
def news_status():
    return {**news.counts(), **news.queue_info(), "rss_last": news.status["rss_last"], "rss_error": news.status["rss_error"],
            "gdelt_error": news.status["gdelt_error"], "offline": config.OFFLINE}


@app.get("/api/news/impact")
def news_impact(q: str, symbol: str = "NIFTY 50"):
    _sym(symbol)
    if len(q.strip()) < 2:
        raise HTTPException(400, "type at least 2 characters")
    return news.impact(q.strip(), symbol)


# ───────────────────────────── pattern coach ─────────────────────────────
@app.get("/api/harmonics/scan")
def harmonics_scan(symbol: str, tf: str = "15", end: int | None = None, bars: int = Query(400, ge=50, le=3000),
                   piv: int = Query(3, ge=2, le=10), tol: float = Query(8.0, ge=1, le=20)):
    """Completed + forming harmonic patterns in the last `bars` bars up to `end` (replay clock: only data known then)."""
    _sym(symbol), _tf(tf)
    df = store.bars(symbol, tf)
    if end is not None:
        df = df[df.index < store.from_epoch(end)]            # end = knownUntil: bars that have CLOSED by then
    return harmonic_scan.scan(df.iloc[-bars:], piv_len=piv, tol=tol / 100)


@app.get("/api/harmonics/day")
def harmonics_day(symbol: str, date: str, tf: str = "5", piv: int = Query(3, ge=2, le=10), tol: float = Query(8.0, ge=1, le=20)):
    """Every harmonic pattern whose D completed on `date` (YYYY-MM-DD), using the days before for X/A context and the
    days after for the outcome. Also returns the chart window to show."""
    _sym(symbol), _tf(tf)
    try:
        day = pd.Timestamp(date).normalize()
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    df = store.bars(symbol, tf)
    win = df[(df.index >= day - pd.Timedelta(days=6)) & (df.index < day + pd.Timedelta(days=3))]
    if win.empty or not (win.index.normalize() == day).any():
        raise HTTPException(404, f"no {symbol} {tf}m data on {date} (holiday, weekend or outside the data)")
    r = harmonic_scan.scan(win, piv_len=piv, tol=tol / 100, max_items=500)
    d0, d1 = int(store.to_epoch(pd.DatetimeIndex([day]))[0]), int(store.to_epoch(pd.DatetimeIndex([day + pd.Timedelta(days=1)]))[0])
    items = [x for x in r["completed"] if d0 <= x["points"][-1]["time"] < d1]
    return {"date": date, "patterns": items, "window": [int(store.to_epoch(win.index[:1])[0]), int(store.to_epoch(win.index[-1:])[0])]}


@app.get("/api/harmonics/days")
def harmonics_days(symbol: str, tf: str = "5", start: str | None = None, end: str | None = None,
                   piv: int = Query(3, ge=2, le=10), top: int = Query(40, ge=1, le=300)):
    """Rank trading days by how many harmonic patterns completed on them (to find good days to study)."""
    _sym(symbol), _tf(tf)
    df = store.bars(symbol, tf)
    e = pd.Timestamp(end) + pd.Timedelta(days=1) if end else df.index[-1] + pd.Timedelta(minutes=1)
    s_ = pd.Timestamp(start) if start else e - pd.Timedelta(days=365)
    if (e - s_).days > 3 * 366:
        raise HTTPException(400, "range too long — pick 3 years or less")
    part = df[(df.index >= s_ - pd.Timedelta(days=3)) & (df.index < e)]
    if part.empty:
        raise HTTPException(404, "no data in that range")
    r = harmonic_scan.scan(part, piv_len=piv, max_items=10**9)
    days = {}
    s0 = int(store.to_epoch(pd.DatetimeIndex([s_]))[0])
    for x in r["completed"]:
        t = x["points"][-1]["time"]
        if t < s0:
            continue
        d = store.from_epoch(t).strftime("%Y-%m-%d")
        g = days.setdefault(d, {"date": d, "count": 0, "patterns": {}, "worked": 0, "bull": 0, "bear": 0})
        g["count"] += 1
        g["patterns"][x["pattern"]] = g["patterns"].get(x["pattern"], 0) + 1
        g["worked"] += 1 if (x["reaction"] or {}).get("t1_hit") else 0
        g["bull" if x["dir"] == "bullish" else "bear"] += 1
    ranked = sorted(days.values(), key=lambda g: (-g["count"], -len(g["patterns"]), g["date"]))
    return {"symbol": symbol, "tf": tf, "days_with_patterns": len(days), "total_patterns": sum(g["count"] for g in days.values()),
            "top": ranked[:top]}


@app.get("/api/harmonics/challenge")
def harmonics_challenge(symbol: str, tf: str = "15", after: int = 0, piv: int = Query(3, ge=2, le=10)):
    """Next pattern-challenge round after time `after` (the UI hides `app` and `actual` until the reveal)."""
    _sym(symbol), _tf(tf)
    df = store.bars(symbol, tf)
    i0 = int(df.index.searchsorted(store.from_epoch(after)))
    for chunk in range(6):                                   # look ahead in windows until a setup is found
        a = max(0, i0 - 400 + chunk * 1500)
        part = df.iloc[a: a + 2400]
        if len(part) < 60:
            break
        r = harmonic_scan.next_setup(part, after, piv_len=piv)
        if r:
            return r
    raise HTTPException(404, "no pattern setup found after that date — try another date")


@app.get("/api/harmonics/library")
def harmonics_library(symbol: str, tf: str = "5", start: str | None = None, end: str | None = None, pattern: str | None = None,
                      outcome: str = Query("all", pattern="^(all|worked|failed|open)$"), dir: str = Query("all", pattern="^(all|bullish|bearish)$"),
                      piv: int = Query(3, ge=2, le=10), limit: int = Query(300, ge=1, le=2000)):
    """Every real completed harmonic pattern in a date range (≤ 3 years), one row per X-A-B-C-D (patterns that share the
    same points are merged: a Bat that is also a Gartley lists both). Outcome: worked = price came back ≥ 38.2% of AD
    within 30 bars, failed = it did not, open = not enough bars yet. `broke_x` = price went beyond X within 30 bars."""
    _sym(symbol), _tf(tf)
    df = store.bars(symbol, tf)
    e = pd.Timestamp(end) + pd.Timedelta(days=1) if end else df.index[-1] + pd.Timedelta(minutes=1)
    s_ = pd.Timestamp(start) if start else e - pd.Timedelta(days=365)
    if (e - s_).days > 3 * 366:
        raise HTTPException(400, "range too long — pick 3 years or less")
    part = df[(df.index >= s_ - pd.Timedelta(days=5)) & (df.index < e + pd.Timedelta(days=5))]
    if part.empty:
        raise HTTPException(404, "no data in that range")
    r = harmonic_scan.scan(part, piv_len=piv, max_items=10**9)
    s0, e0 = int(store.to_epoch(pd.DatetimeIndex([s_]))[0]), int(store.to_epoch(pd.DatetimeIndex([e]))[0])
    t_all = store.to_epoch(part.index)
    groups = {}
    for x in r["completed"]:
        dt = x["points"][-1]["time"]
        if not (s0 <= dt < e0):
            continue
        k = (x["dir"], tuple(p["time"] for p in x["points"]))
        g = groups.get(k)
        if g is None:
            rx = x["reaction"] or {}
            st = "open" if not rx or not rx.get("complete") else "worked" if rx.get("t1_hit") else "failed"
            g = groups[k] = {"points": x["points"], "dir": x["dir"], "patterns": [], "keys": [], "checks": {}, "score": 0,
                             "reaction": x["reaction"], "outcome": st, "known_at": x["known_at"], "broke_x": None}
            if len(x["points"]) == 5:
                d_i = int(np.searchsorted(t_all, dt))
                seg = part.iloc[d_i + 1: d_i + 31]
                X = x["points"][0]["price"]
                g["broke_x"] = bool(len(seg) and ((seg.low.min() < X) if x["dir"] == "bullish" else (seg.high.max() > X)))
        g["patterns"].append(x["pattern"]); g["keys"].append(x["key"]); g["checks"][x["pattern"]] = x["checks"]
        g["score"] = max(g["score"], x["score"])
    items = sorted(groups.values(), key=lambda g: -g["points"][-1]["time"])
    counts = {"total": len(items), "worked": sum(g["outcome"] == "worked" for g in items), "failed": sum(g["outcome"] == "failed" for g in items),
              "by_pattern": {}}
    for g in items:
        for n in g["patterns"]:
            counts["by_pattern"][n] = counts["by_pattern"].get(n, 0) + 1
    if pattern:
        items = [g for g in items if pattern in g["patterns"] or pattern in g["keys"]]
    if outcome != "all":
        items = [g for g in items if g["outcome"] == outcome]
    if dir != "all":
        items = [g for g in items if g["dir"] == dir]
    return {"symbol": symbol, "tf": tf, "start": s_.strftime("%Y-%m-%d"), "end": (e - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            "counts": counts, "matched": len(items), "items": items[:limit]}


PRACTICE = config.DATA_DIR / "harmonic_practice.json"
PRACTICE_KEYS = {"id", "at", "mode", "pattern", "answer", "ok", "points", "status", "R", "pts", "symbol", "tf", "secs", "drill", "note", "score"}


@app.get("/api/practice")
def get_practice():
    """Harmonic practice log (check my drawing, plan drill, flashcards, daily drill) — feeds the practice stats."""
    return json.loads(PRACTICE.read_text()) if PRACTICE.exists() else []


@app.post("/api/practice")
def add_practice(entry: dict = Body(...)):
    log = get_practice()
    e = {k: entry[k] for k in entry if k in PRACTICE_KEYS}
    if len(json.dumps(e)) > 20_000:
        raise HTTPException(413, "entry too large")
    e.setdefault("at", pd.Timestamp.now().strftime("%Y-%m-%dT%H:%M:%S"))
    log.append(e)
    PRACTICE.write_text(json.dumps(log[-20000:]))
    return {"count": len(log)}


@app.delete("/api/practice")
def clear_practice():
    PRACTICE.write_text("[]")
    return {"count": 0}


# ───────────────────────────── backtest ─────────────────────────────
@app.post("/api/backtest/{ind_id}")
def run_backtest(ind_id: str, body: dict = Body(...)):
    symbol, tf = body.get("symbol", "NIFTY 50"), body.get("tf", "15")
    _sym(symbol), _tf(tf)
    keys = ("start", "end", "params", "sl", "tp", "unit", "max_per_day", "entry_from", "entry_to", "qty", "cost_pct", "side")
    try:
        return backtest.run(ind_id, symbol, tf, **{k: body[k] for k in keys if k in body and body[k] is not None})
    except KeyError as e:
        raise HTTPException(404, str(e))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


# ───────────────────────────── indicators ─────────────────────────────
@app.get("/api/indicators")
def indicators():
    inds = registry.all_indicators()
    return {"indicators": [{**i.meta(), "source": getattr(i, "source", "builtin"), "file": getattr(i, "file", "")}
                           for i in inds.values()], "errors": registry.errors()}


_icache: "OrderedDict[str, dict]" = OrderedDict()
_ilock = threading.Lock()


def compute_indicator(ind_id, symbol, tf, start=None, end=None, params=None):
    ind = registry.get(ind_id)
    p = ind.resolve(params)
    df = store.bars(symbol, tf)
    if start is not None:
        i0 = int(df.index.searchsorted(store.from_epoch(start)))
        j0 = max(0, i0 - ind.warmup)
        if j0 > 0:                                     # start the warm-up at a day boundary (day-level indicators)
            day0 = df.index[j0].normalize() - pd.Timedelta(days=1)
            j0 = int(df.index.searchsorted(day0))
        df = df.iloc[j0:]
    if end is not None:
        df = df[df.index <= store.from_epoch(end)]
    key = json.dumps([ind_id, symbol, tf, start, end, p, len(df), str(df.index[-1]) if len(df) else "",
                      float(df.close.iloc[-1]) if len(df) else 0, getattr(ind, "file", "")], sort_keys=True, default=str)
    with _ilock:
        if key in _icache:
            _icache.move_to_end(key)
            return _icache[key]
    df.attrs["symbol"] = symbol                    # for indicators that need finer bars of the same symbol
    out = ind.compute(df, **p).to_dict()
    if start is not None:
        out["series"] = [{**s, "data": [d for d in s["data"] if d["time"] >= start]} for s in out["series"]]
        out["markers"] = [m for m in out["markers"] if m["time"] >= start]
        out["shapes"] = [s for s in out["shapes"] if max((pt["time"] for pt in s["points"]), default=0) >= start]
    out["markers"].sort(key=lambda m: m["time"])
    out["params"] = p
    with _ilock:
        _icache[key] = out
        while len(_icache) > 64:
            _icache.popitem(last=False)
    return out


@app.get("/api/edge")
def edge_status():
    """Morning Edge: background-job state, today's 09:45 decision and the model's training info."""
    m = edge.load_model()
    today = edge.DIR / "signals" / f"{sync.now_ist().date()}.json"
    return {**edge.status, "model": m["meta"] if m else None,
            "today": json.loads(today.read_text()) if today.exists() else edge.status.get("today") or _edge_pending()}


def _edge_pending():
    now = sync.now_ist()
    m = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return {"note": "weekend — no decision"}
    if m < 9 * 60 + 45:
        return {"note": "decision comes at 09:45–09:52"}
    if m <= 9 * 60 + 52:
        return {"note": "deciding now (09:45–09:52)…"}
    return {"note": "no decision today — the 09:45–09:52 window was missed (ChartLab or live data was down) or it was a "
                    "holiday; the day is skipped and not counted"}


@app.post("/api/indicators/{ind_id}/compute")
def compute(ind_id: str, body: dict = Body(...)):
    symbol, tf = body.get("symbol", "NIFTY 50"), body.get("tf", "5")
    _sym(symbol), _tf(tf)
    try:
        return compute_indicator(ind_id, symbol, tf, body.get("start"), body.get("end"), body.get("params"))
    except KeyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:                             # a broken custom indicator must not take the app down
        log.exception("indicator %s", ind_id)
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=422)


# ───────────────────────────── drawings & journal ─────────────────────────────
def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:80]


@app.get("/api/drawings")
def get_drawings(symbol: str):
    p = config.DATA_DIR / "drawings" / f"{_safe(symbol)}.json"
    return json.loads(p.read_text()) if p.exists() else []


@app.put("/api/drawings")
def put_drawings(symbol: str, items: list = Body(...)):
    if len(json.dumps(items)) > 2_000_000:
        raise HTTPException(413, "too many drawings")
    p = config.DATA_DIR / "drawings" / f"{_safe(symbol)}.json"
    p.write_text(json.dumps(items))
    return {"saved": len(items)}


JOURNAL = config.DATA_DIR / "journal.json"


@app.get("/api/journal")
def get_journal():
    return json.loads(JOURNAL.read_text()) if JOURNAL.exists() else []


@app.post("/api/journal")
def add_journal(trade: dict = Body(...)):
    j = get_journal()
    trade = {k: trade[k] for k in trade if k in {"id", "symbol", "tf", "side", "qty", "entry", "exit", "entry_time", "exit_time",
                                                 "pnl_pts", "pnl_rs", "reason", "mode", "note", "sl", "tp", "session", "tag", "instrument", "blind"}}
    j.append(trade)
    JOURNAL.write_text(json.dumps(j[-5000:]))
    return {"count": len(j)}


@app.delete("/api/journal")
def clear_journal():
    JOURNAL.write_text("[]")
    return {"count": 0}


# ───────────────────────────── live feed ─────────────────────────────
class LiveHub:
    """One polling task per symbol while somebody watches it. Pushes the latest bars for each subscriber's timeframe."""

    def __init__(self):
        self.subs: dict = {}          # symbol → set[(websocket, tf)]
        self.tasks: dict = {}
        self.today: dict = {}         # symbol → DataFrame of today's 1-minute bars

    async def join(self, ws, symbol, tf):
        self.subs.setdefault(symbol, set()).add((ws, tf))
        if symbol not in self.tasks or self.tasks[symbol].done():
            self.tasks[symbol] = asyncio.create_task(self.loop(symbol))
        if symbol in self.today:
            await self.push_one(ws, symbol, tf)

    def leave(self, ws, symbol, tf):
        self.subs.get(symbol, set()).discard((ws, tf))

    async def push_one(self, ws, symbol, tf):
        d = self.today.get(symbol)
        if d is None or d.empty:
            return
        bars = store.records(store.resample(d, tf, store.sess(symbol)[0]).iloc[-3:])
        await ws.send_json({"type": "bars", "symbol": symbol, "tf": tf, "bars": bars, "source": sources.source_status()["source"],
                            "at": time.time()})

    async def loop(self, symbol):
        loop = asyncio.get_running_loop()
        while self.subs.get(symbol):
            status = {**sources.source_status(), "market_open": sync.market_open(sym=symbol)}
            if not symbols.get(symbol).nse_hours and symbols.get(symbol).exchange != "MCX":
                status.update(source="binance" if symbols.get(symbol).binance else "yahoo",
                              detail=symbols.get(symbol).note)
            if status["market_open"]:
                d = await loop.run_in_executor(None, sync.latest_minutes, symbol)
                if d is not None and len(d):
                    self.today[symbol] = store._clean(d, se=store.sess(symbol))
                    await loop.run_in_executor(None, store.save_ext, symbol, d, "1")
            for ws, tf in list(self.subs.get(symbol, ())):
                try:
                    await ws.send_json({"type": "status", **status})
                    await self.push_one(ws, symbol, tf)
                except Exception:
                    self.leave(ws, symbol, tf)
            await asyncio.sleep((5 if status["source"] in ("angel", "binance") else 30) if status["market_open"] else 60)


hub = LiveHub()


@app.websocket("/ws/live")
async def live(ws: WebSocket, symbol: str = "NIFTY 50", tf: str = "5"):
    await ws.accept()
    if symbol not in symbols.all_symbols() or tf not in store.TIMEFRAMES:
        await ws.close(code=1008)
        return
    await hub.join(ws, symbol, tf)
    try:
        while True:
            await ws.receive_text()                    # client pings; nothing else expected
    except WebSocketDisconnect:
        hub.leave(ws, symbol, tf)


app.mount("/", StaticFiles(directory=str(config.FRONTEND_DIR), html=True), name="frontend")
