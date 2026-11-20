"""Futures volume for NSE indices (the index itself has no volume).

Angel One serves a continuous near-month 1-minute history for the nearest index future (the same way it does for MCX),
back to ~2016. We store only its volume, in CONTRACTS (lots), per minute:
    data/angel_hist/FUTVOL_<ROOT>/1m_<year>.parquet   (index = minute, column volume)
store.minute_bars() fills the index's volume from it, so every timeframe and indicator sees "NIFTY futures volume".
Rollover: each day is scaled by (all contracts ÷ near month) from rollover_factor.parquet (NSE bhavcopy history, then
Angel daily candles of the live contracts), so expiry weeks are not understated.
Live: the futures quote's cumulative traded quantity (shares) ÷ lot size is folded into the current minute
(sync._angel_live); the stored history is topped up after the close by update().
"""
import datetime as dt
import logging
import threading
import time
from pathlib import Path

import pandas as pd

from . import config

log = logging.getLogger("chartlab.futvol")

ROOTS = {"NIFTY 50": "NIFTY", "NIFTY BANK": "BANKNIFTY"}
START_YEAR = 2016
status: dict = {}
_running: set = set()
_lock = threading.Lock()


def _dir(sym: str) -> Path:
    return config.DATA_DIR / "angel_hist" / f"FUTVOL_{ROOTS[sym]}"


def files(sym: str) -> list:
    d = _dir(sym) if sym in ROOTS else None
    return sorted(d.glob("1m_*.parquet")) if d is not None and d.exists() else []


def factor_path(sym: str) -> Path:
    return _dir(sym) / "rollover_factor.parquet"


def factors(sym: str) -> pd.Series:
    """date → (all contracts' volume ÷ near-month volume) that day. Angel's continuous series is the near month only;
    in expiry week the next month trades as much or more (rollover), so near-only volume would understate activity."""
    p = factor_path(sym) if sym in ROOTS else None
    return pd.read_parquet(p).factor if p is not None and p.exists() else pd.Series(dtype="float64")


def load(sym: str, scaled: bool = True) -> pd.Series:
    """Per-minute futures volume (contracts) for an index symbol; empty Series when none is stored.
    scaled: × that day's rollover factor → volume of ALL contracts (days without a factor stay near-month only)."""
    fs = files(sym)
    if not fs:
        return pd.Series(dtype="float64")
    s = pd.concat([pd.read_parquet(f).volume for f in fs])
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if scaled:
        f = factors(sym)
        if len(f):
            k = f.reindex(s.index.normalize()).to_numpy()
            s = s * pd.Series(k, index=s.index).fillna(1.0).clip(lower=1.0, upper=5.0)
    return s


def update_factors(sym: str) -> int:
    """Extend the rollover factors after the last stored date with Angel daily candles of every live contract
    (near + next + far; they cover the days since the NSE file ends). Returns days added."""
    from .sources import Angel
    import time as _t
    api = Angel.api()
    Angel.token(sym)
    fs = Angel._nfo_futures.get(ROOTS[sym]) or []
    if api is None or not fs:
        return 0
    old = factors(sym)
    start = (old.index.max() + pd.Timedelta(days=1)) if len(old) else pd.Timestamp("2026-01-01")
    end = pd.Timestamp(dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30))
    if start.normalize() > end.normalize():
        return 0
    per = {}
    for exp, tok, _sym, _lot in fs:
        _t.sleep(1.1)
        rows = Angel._get(api, {"exchange": "NFO", "symboltoken": tok, "interval": "ONE_DAY",
                                "fromdate": start.strftime("%Y-%m-%d 09:00"), "todate": end.strftime("%Y-%m-%d 15:30")}) or []
        per[exp] = {pd.Timestamp(r[0][:10]): float(r[5]) for r in rows}
    near_exp = min(per)
    days = sorted(set().union(*[set(v) for v in per.values()]))
    new = {}
    for d in days:
        near = per[near_exp].get(d)
        # only days when this contract really was the near month (expiries are ≥ 28 days apart; the expired
        # previous contract is not in Angel's list, so its days cannot be scaled → left out, factor 1.0)
        if not near or d.date() > near_exp or d.date() <= near_exp - dt.timedelta(days=27):
            continue
        new[d] = sum(v.get(d, 0.0) for v in per.values()) / near
    if not new:
        return 0
    f = pd.concat([old, pd.Series(new)]).sort_index()
    f = f[~f.index.duplicated(keep="last")]
    f.to_frame("factor").to_parquet(factor_path(sym))
    return len(new)


def contracts(sym: str) -> list:
    """All live futures of the index: [(expiry, token, trading symbol, lot)], nearest first."""
    from .sources import Angel
    Angel.token(sym)
    return list(Angel._nfo_futures.get(ROOTS[sym]) or [])


def near_contract(sym: str):
    """(expiry, token, trading symbol, lot) of the nearest live future, or None."""
    from .sources import Angel
    Angel.token(sym)                                            # loads the scrip master (futures lists)
    fs = Angel._nfo_futures.get(ROOTS[sym]) or []
    return fs[0] if fs else None


def _fetch(sym: str, a: dt.datetime, b: dt.datetime) -> pd.Series:
    from .sources import Angel
    c = near_contract(sym)
    api = Angel.api()
    if c is None or api is None:
        raise RuntimeError("Angel One / futures token not available")
    out, x = [], a
    while x < b:
        y = min(x + dt.timedelta(days=28), b)
        rows = Angel._get(api, {"exchange": "NFO", "symboltoken": c[1], "interval": "ONE_MINUTE",
                                "fromdate": x.strftime("%Y-%m-%d %H:%M"), "todate": y.strftime("%Y-%m-%d %H:%M")})
        if rows is None:
            raise RuntimeError(Angel._last_error or "candle request failed")
        out += rows
        x = y + dt.timedelta(minutes=1)
    if not out:
        return pd.Series(dtype="float64")
    d = pd.DataFrame(out, columns=["date", "o", "h", "l", "c", "volume"])
    d["date"] = pd.to_datetime(d["date"]).dt.tz_localize(None)
    return d.set_index("date").volume.astype("float64")


def _save(sym: str, year: int, s: pd.Series):
    d = _dir(sym)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"1m_{year}.parquet"
    if p.exists():
        old = pd.read_parquet(p).volume
        s = pd.concat([old, s])
        s = s[~s.index.duplicated(keep="last")]
    tmp = p.with_name(p.name + ".part")
    s.sort_index().to_frame("volume").to_parquet(tmp)
    tmp.replace(p)


def backfill(sym: str) -> dict:
    """Download missing years newest → oldest (a finished past year is skipped; the current year is topped up)."""
    if config.OFFLINE or sym not in ROOTS:
        return {"status": "skipped"}
    with _lock:
        if sym in _running:
            return {"status": "already running"}
        _running.add(sym)
    try:
        now = dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)
        have = {int(f.stem[3:]) for f in files(sym)}
        for year in range(now.year, START_YEAR - 1, -1):
            if year in have and year < now.year:
                continue
            a = dt.datetime(year, 1, 1, 9, 0)
            if year == now.year and year in have:
                last = load(sym)
                a = max(a, last.index.max().to_pydatetime() + dt.timedelta(minutes=1)) if len(last) else a
            b = min(dt.datetime(year, 12, 31, 15, 30), now)
            status[sym] = f"downloading {year}"
            s = _fetch(sym, a, b)
            if len(s) or year < now.year:
                _save(sym, year, s)                     # an empty past year is saved too, so it is not retried
            log.info("futvol %s %d: %d minutes", sym, year, len(s))
            time.sleep(0.5)
        try:
            update_factors(sym)
        except Exception as e:                           # factors are an add-on: never fail the volume download
            log.warning("futvol factors %s: %s", sym, e)
        status[sym] = "done"
        return {"status": "done"}
    except Exception as e:
        status[sym] = f"error: {e}"
        log.warning("futvol %s: %s", sym, e)
        return {"status": f"error: {e}"}
    finally:
        with _lock:
            _running.discard(sym)


def backfill_async(sym: str):
    if sym in ROOTS and sym not in _running:
        threading.Thread(target=backfill, args=(sym,), daemon=True).start()
