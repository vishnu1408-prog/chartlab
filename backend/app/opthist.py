"""Index and commodity option 1-minute history from Angel One, so replay can use real option prices: NIFTY and
SENSEX after the Hugging Face dataset ends (July 2026), and MCX options (crude oil, gold, silver, natural gas), which
have no free history at all.

Angel serves history only for contracts that still trade (the scrip master lists live contracts only), so:
  backfill() — now: every live NIFTY contract within ±BAND points of the index, from its first traded minute
               (walking back 29-day windows until one comes back empty). Long-dated monthlies have traded for months,
               so replay days in Aug–Oct 2026 get real prices from them even where the weekly has expired.
  nightly    — every trading day after the close: today's minutes of the nearest expiries (expiring contracts are
               captured on their last day, before they leave the scrip master) → no gaps from now on.
Files: data/hf/options/<ROOT>/<expiry>.parquet (ROOT = NIFTY, SENSEX, CRUDEOIL, GOLD, SILVER, NATURALGAS), the same columns as the Hugging Face files (timestamp, open, high, low,
close, strike, option_type) + volume, so options.load_chain() reads both. <ROOT>/angel.json lists the expiries
written here and the last minute fetched per contract. Nightly: NSE / BSE after 15:40, MCX after its 23:30 close
(and a morning catch-up if the PC was off). Market data only — never orders.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time

import pandas as pd

from . import config

log = logging.getLogger("chartlab.opthist")
# root: (chart symbol, exchange, strike band as a share of the price, expiries kept up to date every night)
ROOTS = {"NIFTY": ("NIFTY 50", "NFO", 0.065, 4), "SENSEX": ("SENSEX", "BFO", 0.047, 3),
         "CRUDEOIL": ("MCX CRUDEOIL", "MCX", 0.08, 2), "GOLD": ("MCX GOLD", "MCX", 0.06, 3),
         "SILVER": ("MCX SILVER", "MCX", 0.07, 3), "NATURALGAS": ("MCX NATURALGAS", "MCX", 0.12, 3)}
NSE_ROOTS = ("NIFTY", "SENSEX")
MCX_ROOTS = ("CRUDEOIL", "GOLD", "SILVER", "NATURALGAS")
WINDOW = {"NFO": dt.timedelta(days=29), "BFO": dt.timedelta(days=29), "MCX": dt.timedelta(days=9)}   # < 8000 candles
status: dict = {"state": "idle", "root": None, "done": 0, "todo": 0, "rows": 0, "last": None, "error": None}
_lock = threading.Lock()


def _dir(root: str = "NIFTY"):
    return config.HF_DIR / "options" / root


def _meta(root: str = "NIFTY") -> dict:
    p = _dir(root) / "angel.json"
    return json.loads(p.read_text()) if p.exists() else {"expiries": [], "last": {}}


def _save_meta(m: dict, root: str = "NIFTY"):
    _dir(root).mkdir(parents=True, exist_ok=True)
    tmp = _dir(root) / "angel.json.tmp"
    tmp.write_text(json.dumps(m, indent=1))
    tmp.replace(_dir(root) / "angel.json")


def _fetch(api, token: str, a: dt.datetime, b: dt.datetime, seg: str = "NFO") -> pd.DataFrame:
    from .sources import Angel
    rows = Angel._get(api, attempts=3, params={"exchange": seg, "symboltoken": token, "interval": "ONE_MINUTE",
                                               "fromdate": a.strftime("%Y-%m-%d %H:%M"), "todate": b.strftime("%Y-%m-%d %H:%M")})
    if not rows:
        return pd.DataFrame()
    d = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    t = pd.to_datetime(d.timestamp)
    d["timestamp"] = t.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None) if t.dt.tz is not None else t
    return d


def merge(expiry: dt.date, new: pd.DataFrame, root: str = "NIFTY"):
    """Add rows to the expiry's parquet (new rows win on the same minute / strike / type)."""
    if new.empty:
        return
    p = _dir(root) / f"{expiry.isoformat()}.parquet"
    if p.exists():
        old = pd.read_parquet(p)
        new = pd.concat([old, new], ignore_index=True)
    new = (new.drop_duplicates(["timestamp", "strike", "option_type"], keep="last")
              .sort_values(["option_type", "strike", "timestamp"]).reset_index(drop=True))
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    new.to_parquet(tmp, index=False)
    tmp.replace(p)
    from . import options
    with options._lock:                                      # drop a cached copy of this chain
        options._chains.pop((root, expiry), None)


def _contracts(root: str, max_expiries: int | None):
    from . import chain
    from .sources import Angel
    sym, seg, band, _ = ROOTS[root]
    _, name, rows = chain.contracts(sym)
    exps = sorted({r[0] for r in rows})[:max_expiries] if max_expiries else sorted({r[0] for r in rows})
    if not exps:
        return [], None
    ex, tok, _ = chain.underlying(sym, seg, name, exps[0])     # index, or the MCX futures the options are on
    q = Angel.quotes({ex: [tok]}, "LTP") if tok else {}
    spot = next((float(v["ltp"]) for v in q.values() if v.get("ltp")), None)
    if spot is None:
        raise RuntimeError(f"no {sym} price from Angel")
    sel = [r for r in rows if r[0] in exps and abs(r[1] - spot) <= band * spot]
    return sel, spot


def run(full: bool, roots: tuple = NSE_ROOTS, max_expiries: int | None = None, since: dt.datetime | None = None) -> dict:
    """full=True: walk back to each contract's first traded minute (or `since`); False: only after the last stored
    minute (nightly top-up, the nearest expiries of each root)."""
    from .sources import Angel
    from .sync import now_ist
    if config.OFFLINE or not Angel.available():
        return {"error": "Angel One not available"}
    if not _lock.acquire(blocking=False):
        return {"error": "already running"}
    try:
        api = Angel.api()
        status.update(rows=0, error=None)
        for root in roots:
            _run_root(api, root, full, max_expiries if max_expiries else (None if full else ROOTS[root][3]), since, now_ist())
        status.update(state="idle", root=None, last=now_ist().isoformat(timespec="minutes"))
        return dict(status)
    except Exception as e:
        log.exception("option history")
        status.update(state="idle", error=str(e)[:200])
        return {"error": str(e)}
    finally:
        _lock.release()


def _run_root(api, root: str, full: bool, max_expiries, since, now):
    sym, seg, _, _ = ROOTS[root]
    sel, spot = _contracts(root, max_expiries)
    meta = _meta(root)
    now = now.replace(second=0, microsecond=0)
    win = WINDOW[seg]
    status.update(state="fetching", root=root, done=0, todo=len(sel), spot=spot)
    by_exp: dict = {}
    for exp, k, typ, tok, tsym, lot in sel:
        key = tsym
        last = meta["last"].get(key)
        stop_at = (pd.Timestamp(last).to_pydatetime() + dt.timedelta(minutes=1)) if (last and not full) else since
        parts, b = [], now
        while True:
            a = max(b - win, stop_at) if stop_at else b - win
            d = _fetch(api, str(tok), a, b, seg)
            if d.empty:
                break
            parts.append(d)
            if stop_at and a <= stop_at:
                break
            b = a - dt.timedelta(minutes=1)
        if parts:
            d = pd.concat(parts, ignore_index=True)
            d["strike"], d["option_type"] = float(k), typ
            by_exp.setdefault(exp, []).append(d)
            meta["last"][key] = str(d.timestamp.max())
            status["rows"] += len(d)
        status["done"] += 1
        if len(by_exp.get(exp, [])) >= 20:                # write in batches: a crash keeps what was fetched
            merge(exp, pd.concat(by_exp.pop(exp), ignore_index=True), root)
            meta["expiries"] = sorted(set(meta["expiries"]) | {exp.isoformat()})
            _save_meta(meta, root)
    for exp, ds in by_exp.items():
        merge(exp, pd.concat(ds, ignore_index=True), root)
        meta["expiries"] = sorted(set(meta["expiries"]) | {exp.isoformat()})
    _save_meta(meta, root)
    log.info("option history %s %s: %d contracts", root, "backfill" if full else "nightly", len(sel))


def run_async(full: bool, **kw):
    kw["roots"] = tuple(kw.get("roots") or NSE_ROOTS)
    threading.Thread(target=run, args=(full,), kwargs=kw, daemon=True, name="opthist").start()


def loop():
    """Nightly top-ups: NSE / BSE options 15:40–20:00, MCX options 23:35–23:59, and an MCX morning catch-up
    (06:00–08:50) for a night the PC was off — each once per trading day (Mon–Fri sessions)."""
    from .sync import now_ist
    done: dict = {}
    while True:
        try:
            t = now_ist()
            m = t.hour * 60 + t.minute
            if t.weekday() < 5 and 15 * 60 + 40 <= m <= 20 * 60 and done.get("nse") != t.date():
                if "error" not in run(False, roots=NSE_ROOTS):
                    done["nse"] = t.date()
            mcx_day = t.date() if m >= 23 * 60 + 35 else t.date() - dt.timedelta(days=1)
            mcx_due = (m >= 23 * 60 + 35 or 6 * 60 <= m <= 8 * 60 + 50) and mcx_day.weekday() < 5
            if mcx_due and done.get("mcx") != mcx_day:
                if "error" not in run(False, roots=MCX_ROOTS):
                    done["mcx"] = mcx_day
        except Exception:
            log.exception("opthist loop")
        time.sleep(300)


def start():
    if config.OFFLINE:
        return
    threading.Thread(target=loop, daemon=True, name="opthist").start()
