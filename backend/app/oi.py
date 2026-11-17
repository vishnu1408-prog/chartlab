"""Open-interest dashboard + F&O stock screener (live from Angel One quotes; previous day from NSE's F&O bhavcopy).

  prev-day OI  : the full NSE F&O bhavcopy of the last trading day (UDiFF) → data/oi/fo_<date>.parquet
                 (sym, kind FUT/CE/PE, expiry, strike, oi, close). Used for "OI change vs yesterday".
                 SENSEX (BSE) has no file here → its change is measured from the first snapshot ChartLab saw today.
  chain        : every strike within ±8 % of the index: CE / PE OI (lots), change, LTP, IV → max pain, PCR, call / put
                 walls (largest CE OI above / PE OI below the price), ATM IV.
  recorder     : every minute in market hours: NIFTY (nearest + next expiry), SENSEX, BANKNIFTY — the whole chain
                 (OI, change, LTP, volume, IV per strike) + its summary (PCR / max pain / walls); every 5 minutes the
                 stock screener; any chain opened on the OI page (e.g. a stock's) is saved too. After the close the
                 day's NSE F&O bhavcopy (end-of-day OI of every index + stock option) is kept in data/oi.
                 All of it → data/oi/oi_history.db (separate from chartlab.db so backups stay small) → rewind any
                 minute later ("at=").
  screener     : all F&O stocks every minute: price, % change, gap, place in the day's range, value traded vs the
                 20-day average (so far today), near-month futures OI change → long / short buildup, short covering,
                 long unwinding.
Market data only — never orders.
"""
from __future__ import annotations

import datetime as dt
import contextlib
import io
import logging
import sqlite3
import threading
import time
import zipfile

import numpy as np
import pandas as pd

from . import config, db

log = logging.getLogger("chartlab.oi")
DIR = config.DATA_DIR / "oi"
INDEX = {"NIFTY 50": ("NFO", "NIFTY", "NSE"), "SENSEX": ("BFO", "SENSEX", "BSE"), "NIFTY BANK": ("NFO", "BANKNIFTY", "NSE")}
status: dict = {"state": "idle", "error": None, "screener_at": None}
_first: dict = {}             # (date, token) → first OI seen today (lots) — baseline where no bhavcopy exists
_screen: dict = {"rows": [], "at": None}
_lock = threading.Lock()

HDB = DIR / "oi_history.db"
SCHEMA = """CREATE TABLE IF NOT EXISTS oi_snap (symbol TEXT, expiry TEXT, ts TEXT, spot REAL, pcr REAL, max_pain REAL,
    ce_oi REAL, pe_oi REAL, call_wall REAL, put_wall REAL, atm_iv REAL, lot REAL, atm REAL, since TEXT,
    PRIMARY KEY (symbol, expiry, ts));
CREATE TABLE IF NOT EXISTS oi_chain (symbol TEXT, expiry TEXT, ts TEXT, strike REAL,
    ce_oi REAL, ce_chg REAL, ce_ltp REAL, ce_vol REAL, ce_iv REAL, pe_oi REAL, pe_chg REAL, pe_ltp REAL, pe_vol REAL, pe_iv REAL,
    PRIMARY KEY (symbol, expiry, ts, strike));
CREATE TABLE IF NOT EXISTS screener_snap (ts TEXT, sym TEXT, ltp REAL, chg REAL, gap REAL, pos REAL, high REAL, low REAL,
    vol_ratio REAL, oi_chg REAL, buildup TEXT, lot REAL, PRIMARY KEY (ts, sym));
CREATE INDEX IF NOT EXISTS oi_snap_day ON oi_snap (symbol, ts);"""
CHAIN_COLS = ("oi", "chg", "ltp", "vol", "iv")
_hinit = {"done": False}
_saved: dict = {}             # (symbol, expiry) → last minute saved (one chain per minute)


@contextlib.contextmanager
def hconn():
    """Connection to the OI history database (created / migrated on first use)."""
    DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(HDB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    try:
        if not _hinit["done"]:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            try:                                     # first run: bring over the summaries recorded in chartlab.db
                with db.conn() as old:
                    rows = [tuple(r) for r in old.execute("SELECT * FROM oi_snap")]
                c.executemany("INSERT OR IGNORE INTO oi_snap (symbol, expiry, ts, spot, pcr, max_pain, ce_oi, pe_oi, "
                              "call_wall, put_wall, atm_iv) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            except Exception:
                pass
            _hinit["done"] = True
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


# ───────────────────────────── previous-day OI (NSE bhavcopy) ─────────────────────────────
def _prev_day(now: dt.datetime) -> dt.date:
    d = now.date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def prev_oi(now: dt.datetime | None = None) -> pd.DataFrame:
    """Full F&O bhavcopy of the last trading day before `now` (tries up to 6 days back for holidays)."""
    from .sync import now_ist
    now = now or now_ist()
    DIR.mkdir(parents=True, exist_ok=True)
    d = _prev_day(now)
    for _ in range(6):
        p = DIR / f"fo_{d}.parquet"
        if p.exists():
            return pd.read_parquet(p)
        if not config.OFFLINE:
            from .edge import _get
            raw = _get(f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
            if raw:
                z = zipfile.ZipFile(io.BytesIO(raw))
                x = pd.read_csv(z.open(z.namelist()[0]), low_memory=False)
                x.columns = [c.strip() for c in x.columns]
                x = x[x.FinInstrmTp.isin(["IDF", "IDO", "STF", "STO"])]
                out = pd.DataFrame({"sym": x.TckrSymb.astype(str), "kind": x.OptnTp.where(x.FinInstrmTp.isin(["IDO", "STO"]), "FUT"),
                                    "expiry": pd.to_datetime(x.XpryDt).dt.date.astype(str), "strike": x.StrkPric.fillna(0).astype(float),
                                    "oi": x.OpnIntrst.astype(float), "close": x.ClsPric.astype(float),
                                    "lot": x.get("NewBrdLotQty", pd.Series(np.nan, index=x.index)).astype(float)})
                out.to_parquet(p, index=False)
                return out
        d -= dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
    return pd.DataFrame(columns=["sym", "kind", "expiry", "strike", "oi", "close", "lot"])


# ───────────────────────────── maths (pure) ─────────────────────────────
def max_pain(strikes: np.ndarray, ce_oi: np.ndarray, pe_oi: np.ndarray) -> float:
    """The expiry price at which option BUYERS collect the least (sum of CE + PE intrinsic value × OI)."""
    k = np.asarray(strikes, float)
    pain = [(np.maximum(s - k, 0) * ce_oi).sum() + (np.maximum(k - s, 0) * pe_oi).sum() for s in k]
    return float(k[int(np.argmin(pain))])


def buildup(price_chg: float, oi_chg: float) -> str:
    if price_chg is None or oi_chg is None or not np.isfinite(price_chg) or not np.isfinite(oi_chg):
        return "—"
    if oi_chg > 0:
        return "Long buildup" if price_chg > 0 else "Short buildup"
    return "Short covering" if price_chg > 0 else "Long unwinding"


# ───────────────────────────── option chain snapshot ─────────────────────────────
def chain(symbol: str, expiry: str | None = None, band: float = 0.08) -> dict:
    """Full chain within ±band of the price for an index or an F&O stock."""
    from . import paper, sources
    from .chain import implied_vol, years_left
    from .sync import now_ist
    now = now_ist()
    seg, name, rows = paper.contracts(symbol)
    exps = sorted({r[0] for r in rows if r[0] >= now.date()})
    if not exps:
        return {"error": f"no live option contracts for {symbol}"}
    exp = dt.date.fromisoformat(expiry) if expiry and dt.date.fromisoformat(expiry) in exps else exps[0]
    q0, under = paper.quotes([], (symbol,))
    spot = under.get(symbol)
    if not spot:
        return {"error": f"no live price for {symbol}"}
    sel = [r for r in rows if r[0] == exp and abs(r[1] - spot) <= band * spot]
    lot = sel[0][5] if sel else 1
    raw = sources.Angel.quotes({seg: sorted({str(r[3]) for r in sel})}, "FULL")
    prev = prev_oi(now)
    pv = prev[(prev.sym == name) & (prev.expiry == exp.isoformat())] if len(prev) else prev
    pmap = {(float(k), t): v for k, t, v in zip(pv.strike, pv.kind, pv.oi)} if len(pv) else {}
    T = years_left(now, exp, seg if seg in ("NFO", "BFO", "MCX") else "NFO")
    by: dict = {}
    for e, k, t, tok, tsym, lt in sel:
        qd = raw.get(str(tok))
        if not qd:
            continue
        oi = (qd.get("opnInterest") or 0) / lt
        if pmap:
            base = pmap.get((float(k), t))
            chg = None if base is None else oi - base / lt
            since = "yesterday's close"
        else:
            key = (now.date(), str(tok))
            first = _first.setdefault(key, (oi, now.strftime("%H:%M")))
            chg, since = oi - first[0], f"ChartLab's first look today ({first[1]})"
        ltp = qd.get("ltp")
        by.setdefault(float(k), {})[t] = {"oi": round(oi), "chg": None if chg is None else round(chg), "ltp": ltp,
                                          "vol": round((qd.get("tradeVolume") or 0) / lt),
                                          "iv": implied_vol(ltp, spot, k, T, t == "CE")}
    ks = sorted(by)
    ce = np.array([by[k].get("CE", {}).get("oi", 0) for k in ks], float)
    pe = np.array([by[k].get("PE", {}).get("oi", 0) for k in ks], float)
    above = [k for k in ks if k >= spot]
    below = [k for k in ks if k <= spot]
    cw = max(above, key=lambda k: by[k].get("CE", {}).get("oi", 0)) if above else None
    pw = max(below, key=lambda k: by[k].get("PE", {}).get("oi", 0)) if below else None
    atm = min(ks, key=lambda k: abs(k - spot)) if ks else None
    ivs = [by[atm][t]["iv"] for t in ("CE", "PE") if atm is not None and t in by[atm] and by[atm][t]["iv"]]
    return {"symbol": symbol, "expiry": exp.isoformat(), "expiries": [x.isoformat() for x in exps], "spot": spot, "lot": lot,
            "at": now.strftime("%H:%M:%S"), "since": since if by else None,
            "rows": [{"strike": k, "ce": by[k].get("CE"), "pe": by[k].get("PE")} for k in ks],
            "pcr": round(float(pe.sum() / ce.sum()), 3) if ce.sum() else None,
            "max_pain": max_pain(np.array(ks), ce, pe) if ks else None,
            "call_wall": cw, "put_wall": pw, "atm": atm,
            "atm_iv": round(float(np.mean(ivs)), 1) if ivs else None,
            "ce_oi": float(ce.sum()), "pe_oi": float(pe.sum())}


def save_chain(c: dict, ts: dt.datetime) -> bool:
    """Store a live chain (summary + every strike) for its minute; once per symbol / expiry / minute."""
    if not c or "error" in c or not c.get("rows"):
        return False
    m = ts.replace(second=0, microsecond=0).isoformat()
    key = (c["symbol"], c["expiry"])
    if _saved.get(key) == m:
        return False
    rows = [(c["symbol"], c["expiry"], m, r["strike"],
             *[(r.get("ce") or {}).get(k) for k in CHAIN_COLS], *[(r.get("pe") or {}).get(k) for k in CHAIN_COLS])
            for r in c["rows"]]
    with hconn() as con:
        con.execute("INSERT OR REPLACE INTO oi_snap VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (c["symbol"], c["expiry"], m, c["spot"], c["pcr"], c["max_pain"], c["ce_oi"], c["pe_oi"],
                     c["call_wall"], c["put_wall"], c["atm_iv"], c.get("lot"), c.get("atm"), c.get("since")))
        con.executemany("INSERT OR REPLACE INTO oi_chain VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    _saved[key] = m
    return True


def live_chain(symbol: str, expiry: str | None = None) -> dict:
    """chain() + save it (so whatever is looked at live can be rewound later)."""
    from .sync import market_open, now_ist
    c = chain(symbol, expiry)
    now = now_ist()
    if "error" not in c and market_open(now, "NIFTY 50"):
        try:
            save_chain(c, now)
        except Exception as e:
            log.warning("oi save %s: %s", symbol, e)
    return c


def snap(symbol: str, expiry: str | None = None) -> dict | None:
    from .sync import now_ist
    c = chain(symbol, expiry)
    if "error" in c:
        return None
    save_chain(c, now_ist())
    return c


def history(symbol: str, day: str | None = None, expiry: str | None = None) -> list:
    """Summary rows of a day (PCR, max pain, walls…). Without an expiry: the nearest one recorded that day."""
    from .sync import now_ist
    day = day or now_ist().date().isoformat()
    with hconn() as con:
        if not expiry:
            r = con.execute("SELECT MIN(expiry) e FROM oi_snap WHERE symbol=? AND ts LIKE ? AND expiry >= ?",
                            (symbol, f"{day}%", day)).fetchone()
            expiry = r["e"] if r else None
        return [dict(r) for r in con.execute("SELECT * FROM oi_snap WHERE symbol=? AND expiry=? AND ts LIKE ? ORDER BY ts",
                                             (symbol, expiry, f"{day}%"))]


def days(symbol: str) -> list:
    """Days with recorded chains (newest first) + days with an end-of-day bhavcopy file."""
    with hconn() as con:
        rec = [r["d"] for r in con.execute("SELECT DISTINCT substr(ts,1,10) d FROM oi_snap WHERE symbol=? ORDER BY d DESC",
                                           (symbol,))]
    eod = sorted((p.stem[3:] for p in DIR.glob("fo_*.parquet")), reverse=True)
    return {"recorded": rec, "eod": eod}


def times(symbol: str, day: str) -> dict:
    """Recorded minutes of a day per expiry."""
    with hconn() as con:
        out: dict = {}
        for r in con.execute("SELECT expiry, ts FROM oi_snap WHERE symbol=? AND ts LIKE ? ORDER BY ts", (symbol, f"{day}%")):
            out.setdefault(r["expiry"], []).append(r["ts"][11:16])
    return {"symbol": symbol, "day": day, "expiries": out}


def stored_chain(symbol: str, at: str, expiry: str | None = None) -> dict:
    """The chain as it was at `at` (YYYY-MM-DDTHH:MM, IST): the last recorded minute at or before it that day.
    Falls back to the day's end-of-day bhavcopy (OI + close per strike) when nothing was recorded live."""
    day = at[:10]
    with hconn() as con:
        exps = [r["expiry"] for r in con.execute("SELECT DISTINCT expiry FROM oi_snap WHERE symbol=? AND ts LIKE ? ORDER BY expiry",
                                                 (symbol, f"{day}%"))]
        if exps:
            e = expiry if expiry in exps else next((x for x in exps if x >= day), exps[0])
            s = con.execute("SELECT * FROM oi_snap WHERE symbol=? AND expiry=? AND ts LIKE ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
                            (symbol, e, f"{day}%", at + ":59")).fetchone() or \
                con.execute("SELECT * FROM oi_snap WHERE symbol=? AND expiry=? AND ts LIKE ? ORDER BY ts LIMIT 1",
                            (symbol, e, f"{day}%")).fetchone()
            rows = con.execute("SELECT * FROM oi_chain WHERE symbol=? AND expiry=? AND ts=? ORDER BY strike",
                               (symbol, e, s["ts"])).fetchall()
            side = lambda r, p: None if r[f"{p}_oi"] is None and r[f"{p}_ltp"] is None else {k: r[f"{p}_{k}"] for k in CHAIN_COLS}
            return {"symbol": symbol, "expiry": e, "expiries": exps, "spot": s["spot"], "lot": s["lot"],
                    "at": s["ts"][11:16], "ts": s["ts"], "since": s["since"], "replay": True, "source": "recorded",
                    "rows": [{"strike": r["strike"], "ce": side(r, "ce"), "pe": side(r, "pe")} for r in rows],
                    "pcr": s["pcr"], "max_pain": s["max_pain"], "call_wall": s["call_wall"], "put_wall": s["put_wall"],
                    "atm": s["atm"], "atm_iv": s["atm_iv"], "ce_oi": s["ce_oi"], "pe_oi": s["pe_oi"]}
    return eod_chain(symbol, day, expiry)


def eod_chain(symbol: str, day: str, expiry: str | None = None) -> dict:
    """End-of-day chain from NSE's F&O bhavcopy (NSE indices and stocks; not SENSEX)."""
    p = DIR / f"fo_{day}.parquet"
    name = INDEX.get(symbol, (None, symbol))[1]
    if not p.exists():
        return {"error": f"nothing recorded for {symbol} on {day}"}
    x = pd.read_parquet(p)
    fut = x[(x.sym == name) & (x.kind == "FUT")].sort_values("expiry")
    x = x[(x.sym == name) & x.kind.isin(["CE", "PE"]) & (x.expiry >= day)]
    if x.empty:
        return {"error": f"no {symbol} options in the {day} bhavcopy"}
    exps = sorted(x.expiry.unique())
    e = expiry if expiry in exps else exps[0]
    x = x[x.expiry == e]
    lot = float(x.lot.dropna().iloc[0]) if x.lot.notna().any() else 1.0
    spot = float(fut.close.iloc[0]) if len(fut) else None          # near futures close (index close is not in the file)
    by: dict = {}
    for k, t, oi_, cl in zip(x.strike, x.kind, x.oi, x.close):
        by.setdefault(float(k), {})[t] = {"oi": round(oi_ / lot), "chg": None, "ltp": cl, "vol": None, "iv": None}
    ks = sorted(by)
    if spot:
        ks = [k for k in ks if abs(k - spot) <= 0.08 * spot] or ks
    ce = np.array([by[k].get("CE", {}).get("oi", 0) for k in ks], float)
    pe = np.array([by[k].get("PE", {}).get("oi", 0) for k in ks], float)
    above = [k for k in ks if spot and k >= spot]
    below = [k for k in ks if spot and k <= spot]
    return {"symbol": symbol, "expiry": e, "expiries": exps[:8], "spot": spot, "lot": lot, "at": "close", "ts": f"{day}T15:30",
            "since": None, "replay": True, "source": "NSE bhavcopy (end of day; price = near futures close)",
            "rows": [{"strike": k, "ce": by[k].get("CE"), "pe": by[k].get("PE")} for k in ks],
            "pcr": round(float(pe.sum() / ce.sum()), 3) if ce.sum() else None,
            "max_pain": max_pain(np.array(ks), ce, pe) if ks else None,
            "call_wall": max(above, key=lambda k: by[k].get("CE", {}).get("oi", 0)) if above else None,
            "put_wall": max(below, key=lambda k: by[k].get("PE", {}).get("oi", 0)) if below else None,
            "atm": min(ks, key=lambda k: abs(k - spot)) if spot and ks else None, "atm_iv": None,
            "ce_oi": float(ce.sum()), "pe_oi": float(pe.sum())}


# ───────────────────────────── F&O stock screener ─────────────────────────────
def _avg_value() -> dict:
    """20-day average traded value (₹) per stock from the swing data (NSE cash bhavcopy)."""
    p = config.DATA_DIR / "swing" / "work.parquet"
    if not p.exists():
        return {}
    w = pd.read_parquet(p, columns=["date", "sym", "value"])
    last = sorted(w.date.unique())[-20:]
    return w[w.date.isin(last)].groupby("sym").value.mean().to_dict()


_avgv: dict = {"t": 0.0, "v": {}}


def screener() -> list:
    """One row per F&O stock (live). Two quote rounds: cash prices + near-month futures."""
    from . import paper, sources
    from .sync import now_ist
    now = now_ist()
    names = paper.stock_names()
    A = sources.Angel
    eq = {n: (A._tokens or {}).get(("NSE", n)) for n in names}
    fut = {}
    for n in names:
        fs = [f for f in (A._nfo_futures or {}).get(n, []) if f[0] >= now.date()]
        if fs:
            fut[n] = fs[0]
    qe = A.quotes({"NSE": [str(t) for t in eq.values() if t]}, "FULL")
    qf = A.quotes({"NFO": [str(f[1]) for f in fut.values()]}, "FULL")
    prev = prev_oi(now)
    pf = prev[prev.kind == "FUT"] if len(prev) else prev
    poi = {(s, e): o for s, e, o in zip(pf.sym, pf.expiry, pf.oi)} if len(pf) else {}
    if time.time() - _avgv["t"] > 6 * 3600:
        _avgv.update(t=time.time(), v=_avg_value())
    m = now.hour * 60 + now.minute
    frac = min(max((m - 555) / 375, 0.02), 1.0)            # share of the session gone (for value-so-far vs average)
    rows = []
    for n in names:
        q = qe.get(str(eq.get(n))) if eq.get(n) else None
        if not q or not q.get("ltp"):
            continue
        ltp, pc, o, h, l = (float(q.get(k) or 0) for k in ("ltp", "close", "open", "high", "low"))
        chg = (ltp / pc - 1) * 100 if pc else None
        gap = (o / pc - 1) * 100 if pc and o else None
        pos = (ltp - l) / (h - l) if h > l else None
        val = float(q.get("tradeVolume") or 0) * (float(q.get("avgPrice") or 0) or ltp)
        av = _avgv["v"].get(n)
        vr = val / (av * frac) if av else None
        f = fut.get(n)
        oi_chg = None
        if f:
            fq = qf.get(str(f[1]))
            base = poi.get((n, f[0].isoformat()))
            if fq and base:
                oi_chg = ((fq.get("opnInterest") or 0) / base - 1) * 100
        rows.append({"sym": n, "ltp": ltp, "chg": None if chg is None else round(chg, 2),
                     "gap": None if gap is None else round(gap, 2), "pos": None if pos is None else round(pos, 2),
                     "high": h, "low": l, "vol_ratio": None if vr is None else round(vr, 2),
                     "oi_chg": None if oi_chg is None else round(oi_chg, 2), "buildup": buildup(chg, oi_chg),
                     "lot": f[3] if f else None})
    return rows


SCR_COLS = ("sym", "ltp", "chg", "gap", "pos", "high", "low", "vol_ratio", "oi_chg", "buildup", "lot")


def save_screener(rows: list, ts: dt.datetime):
    m = ts.replace(second=0, microsecond=0).isoformat()
    with hconn() as con:
        con.executemany(f"INSERT OR REPLACE INTO screener_snap VALUES ({','.join('?' * 12)})",
                        [(m, *[r.get(k) for k in SCR_COLS]) for r in rows])


def screener_times(day: str) -> list:
    with hconn() as con:
        return [r["ts"][11:16] for r in con.execute("SELECT DISTINCT ts FROM screener_snap WHERE ts LIKE ? ORDER BY ts", (f"{day}%",))]


def screener_days() -> list:
    with hconn() as con:
        return [r["d"] for r in con.execute("SELECT DISTINCT substr(ts,1,10) d FROM screener_snap ORDER BY d DESC")]


def stored_screener(at: str) -> dict:
    with hconn() as con:
        r = con.execute("SELECT MAX(ts) ts FROM screener_snap WHERE ts LIKE ? AND ts <= ?", (f"{at[:10]}%", at + ":59")).fetchone()
        ts = r["ts"] if r and r["ts"] else None
        if ts is None:
            r = con.execute("SELECT MIN(ts) ts FROM screener_snap WHERE ts LIKE ?", (f"{at[:10]}%",)).fetchone()
            ts = r["ts"] if r else None
        if ts is None:
            return {"rows": [], "at": None, "error": f"no screener snapshots on {at[:10]}"}
        rows = [{k: x[k] for k in SCR_COLS} for x in con.execute("SELECT * FROM screener_snap WHERE ts=?", (ts,))]
    return {"rows": rows, "at": ts[11:16], "ts": ts, "replay": True}


def screener_cached(max_age: float = 60) -> dict:
    with _lock:
        if not _screen["rows"] or time.time() - (_screen.get("t") or 0) > max_age:
            try:
                from .sync import now_ist
                _screen.update(rows=screener(), t=time.time(), at=now_ist().strftime("%H:%M:%S"))
                status["error"] = None
            except Exception as e:
                log.exception("screener")
                status["error"] = str(e)[:200]
    return {"rows": _screen["rows"], "at": _screen.get("at")}


# ───────────────────────────── background recorder ─────────────────────────────
RECORD = (("NIFTY 50", 0), ("NIFTY 50", 1), ("SENSEX", 0), ("NIFTY BANK", 0))     # (symbol, expiry index)


def record_once(now: dt.datetime):
    from . import paper
    for sym, i in RECORD:
        try:
            _, _, rows = paper.contracts(sym)
            exps = sorted({r[0] for r in rows if r[0] >= now.date()})
            if len(exps) > i:
                snap(sym, exps[i].isoformat())
        except Exception as e:
            log.warning("oi snap %s: %s", sym, e)
        time.sleep(0.5)                               # spread the quote calls


def loop():
    from .sync import market_open, now_ist
    last_min, last_scr, eod_day = None, 0.0, None
    while True:
        try:
            t = now_ist()
            m = t.replace(second=0, microsecond=0)
            if market_open(t, "NIFTY 50"):
                if m != last_min:
                    last_min = m
                    status["state"] = "recording"
                    record_once(t)
                    status["state"] = "idle"
                if time.time() - last_scr >= 300:
                    last_scr = time.time()
                    try:
                        sc = screener_cached(60)
                        if sc["rows"]:
                            save_screener(sc["rows"], t)
                    except Exception as e:
                        log.warning("screener snap: %s", e)
            elif t.weekday() < 5 and t.hour >= 19 and eod_day != t.date():       # keep today's bhavcopy
                eod_day = t.date()
                if not (DIR / f"fo_{t.date()}.parquet").exists():
                    prev_oi(dt.datetime.combine(t.date() + dt.timedelta(days=3 if t.weekday() == 4 else 1), dt.time(9)))
        except Exception:
            log.exception("oi loop")
        time.sleep(10)


def start():
    if config.OFFLINE:
        return
    threading.Thread(target=loop, daemon=True, name="oi").start()
