"""Footprint data: buy (aggressive buyer, lifted the ask) and sell (aggressive seller, hit the bid) volume per
1-minute × price level, stored per day in data/footprint/<SYMBOL>/<YYYY-MM-DD>.parquet (columns: minute, price, buy, sell).

BTCUSD — Binance BTCUSDT trades (every trade carries its aggressor side, `m` = buyer was the maker → a SELL):
         finished UTC days from the daily archive (data.binance.vision, ~13 MB), the running day from the public REST
         API (data-api.binance.vision) incrementally. Price level = 1 USD.
NIFTY futures / MCX — recorded live by footprint_rec.py from Angel's tick feed (side estimated from bid / ask).
Minutes are IST wall-clock (ChartLab convention). Market data only.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import threading
import time
import zipfile

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger("chartlab.footprint")
DIR = config.DATA_DIR / "footprint"
IST = dt.timedelta(hours=5, minutes=30)
BIN = {"BTCUSD": 1.0, "NIFTY 50": 0.5}                                     # storage price level per symbol (display can be coarser)
_lock = threading.Lock()


def _path(sym: str, day: dt.date):
    return DIR / sym.replace(" ", "_") / f"{day.isoformat()}.parquet"


def aggregate(ts_ms: np.ndarray, price: np.ndarray, qty: np.ndarray, is_sell: np.ndarray, level: float) -> pd.DataFrame:
    """Trades → minute × price-level buy / sell volume. ts in UTC epoch ms; minute stored as IST wall-clock."""
    minute = pd.to_datetime(ts_ms // 60000 * 60000, unit="ms") + IST
    lv = np.floor(price / level) * level
    df = pd.DataFrame({"minute": minute, "price": lv, "buy": np.where(is_sell, 0.0, qty), "sell": np.where(is_sell, qty, 0.0)})
    return df.groupby(["minute", "price"], as_index=False)[["buy", "sell"]].sum()


# ───────────────────────────── BTC (Binance) ─────────────────────────────
def _btc_archive(day: dt.date) -> pd.DataFrame | None:
    """A finished UTC day from the daily aggTrades archive (None if not published yet)."""
    import requests
    u = f"https://data.binance.vision/data/spot/daily/aggTrades/BTCUSDT/BTCUSDT-aggTrades-{day.isoformat()}.zip"
    r = requests.get(u, timeout=180)
    if r.status_code != 200:
        return None
    z = zipfile.ZipFile(io.BytesIO(r.content))
    raw = pd.read_csv(z.open(z.namelist()[0]), header=None,
                      names=["a", "p", "q", "f", "l", "T", "m", "M"], usecols=["p", "q", "T", "m"])
    ts = raw["T"].to_numpy()
    if ts.max() > 1e14:                                    # newer archives use microseconds
        ts = ts // 1000
    return aggregate(ts, raw.p.to_numpy(float), raw.q.to_numpy(float), raw.m.astype(str).str.lower().eq("true").to_numpy()
                     if raw.m.dtype == object else raw.m.to_numpy(bool), BIN["BTCUSD"])


def _btc_rest(start_ms: int, end_ms: int, from_id: int | None) -> tuple[pd.DataFrame, int | None]:
    """Trades between start and end via REST (1000 per call, paged by id). Returns (rows, last id)."""
    from .sources import _binance_get
    out, last = [], from_id
    params = {"symbol": "BTCUSDT", "limit": 1000}
    if from_id is not None:
        params["fromId"] = from_id + 1
    else:
        params.update(startTime=start_ms, endTime=min(end_ms, start_ms + 3_600_000 - 1))
    for _ in range(400):
        k = _binance_get("/api/v3/aggTrades", params)
        if not k:
            if "startTime" in params and params["endTime"] < end_ms:   # empty hour: move on
                params.update(startTime=params["endTime"] + 1, endTime=min(end_ms, params["endTime"] + 3_600_000))
                continue
            break
        out += k
        last = k[-1]["a"]
        if k[-1]["T"] >= end_ms or len(k) < 1000:
            break
        params = {"symbol": "BTCUSDT", "limit": 1000, "fromId": last + 1}
    if not out:
        return pd.DataFrame(columns=["minute", "price", "buy", "sell"]), last
    a = pd.DataFrame(out)
    a = a[a["T"] < end_ms]
    return aggregate(a["T"].to_numpy(), a.p.astype(float).to_numpy(), a.q.astype(float).to_numpy(),
                     a.m.to_numpy(bool), BIN["BTCUSD"]), last


def _merge_save(sym: str, day: dt.date, new: pd.DataFrame):
    p = _path(sym, day)
    if p.exists() and len(new):
        new = pd.concat([pd.read_parquet(p), new]).groupby(["minute", "price"], as_index=False)[["buy", "sell"]].sum()
    elif p.exists():
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    new.to_parquet(tmp, index=False)
    tmp.replace(p)


def ensure_btc(start: dt.datetime, end: dt.datetime, live_hours: float = 3.0) -> dict:
    """Make sure BTC footprint data exists for the IST range [start, end]. Finished UTC days → archive; the running
    UTC day → REST, incrementally (first time only the last `live_hours`)."""
    if config.OFFLINE:
        return {}
    done = {}
    now_utc = dt.datetime.utcnow()
    with _lock:
        d = (start - IST).date()
        while d <= (end - IST).date():
            p = _path("BTCUSD", d)
            st_p = DIR / "BTCUSD" / f"{d.isoformat()}.state.json"
            if d < now_utc.date():
                if not p.exists() or st_p.exists():          # missing, or only partly loaded live → full archive
                    df = _btc_archive(d)
                    if df is not None:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        df.to_parquet(p, index=False)       # replaces a partial live file
                        st_p.unlink(missing_ok=True)
                        done[str(d)] = "archive"
                    else:                                    # archive not out yet (first hours after midnight UTC)
                        done[str(d)] = _btc_live(d, now_utc, live_hours=24)
            else:
                done[str(d)] = _btc_live(d, now_utc, live_hours)
            d += dt.timedelta(days=1)
    return done


def _btc_live(d: dt.date, now_utc: dt.datetime, live_hours: float) -> str:
    st_p = DIR / "BTCUSD" / f"{d.isoformat()}.state.json"
    st = json.loads(st_p.read_text()) if st_p.exists() else {}
    day0 = int(dt.datetime.combine(d, dt.time()).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    end = min(day0 + 86_400_000, int(now_utc.replace(tzinfo=dt.timezone.utc).timestamp() * 1000))
    if st.get("last_id") is None:
        start = max(day0, end - int(live_hours * 3_600_000))
        st["from"] = start
    else:
        start = day0
    df, last = _btc_rest(start, end, st.get("last_id"))
    _merge_save("BTCUSD", d, df)
    st["last_id"] = last
    st_p.parent.mkdir(parents=True, exist_ok=True)
    st_p.write_text(json.dumps(st))
    return f"live ({len(df)} rows)"


def has_data(sym: str | None) -> bool:
    return bool(sym) and (DIR / sym.replace(" ", "_")).exists()


# ───────────────────────────── read + build candles ─────────────────────────────
def load(sym: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Stored minute × level rows for IST [start, end]."""
    d0 = (start - pd.Timedelta(hours=6)).date()
    d1 = (end + pd.Timedelta(hours=6)).date()
    parts = []
    d = d0
    while d <= d1:
        p = _path(sym, d)
        if p.exists():
            parts.append(pd.read_parquet(p))
        d += dt.timedelta(days=1)
    if not parts:
        return pd.DataFrame(columns=["minute", "price", "buy", "sell"])
    x = pd.concat(parts)
    return x[(x.minute >= start) & (x.minute <= end)]


def candles(rows: pd.DataFrame, bar_starts: pd.DatetimeIndex, tf_min: int, level: float) -> dict:
    """minute rows → {bar start: {"rows": [[price, sell, buy], …] (descending price), "delta", "poc", "buy", "sell"}}"""
    if rows.empty or not len(bar_starts):
        return {}
    pos = np.searchsorted(bar_starts.values, rows.minute.values, side="right") - 1
    ok = pos >= 0
    r = rows[ok].copy()
    r["bar"] = bar_starts.values[pos[ok]]
    r = r[(r.minute - r.bar) < pd.Timedelta(minutes=tf_min)]
    r["lv"] = np.floor(r.price / level) * level
    g = r.groupby(["bar", "lv"], as_index=False)[["buy", "sell"]].sum()
    out = {}
    for bar, x in g.groupby("bar"):
        x = x.sort_values("lv", ascending=False)
        tot = x.buy + x.sell
        out[pd.Timestamp(bar)] = {"rows": [[float(p), float(s), float(b)] for p, s, b in zip(x.lv, x.sell, x.buy)],
                                  "delta": float(x.buy.sum() - x.sell.sum()), "buy": float(x.buy.sum()),
                                  "sell": float(x.sell.sum()), "poc": float(x.lv.iloc[int(np.argmax(tot.to_numpy()))])}
    return out
