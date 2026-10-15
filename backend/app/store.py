"""Bar store. Sources, merged with finer data always winning:
  1-minute  Kaggle CSV (cached as parquet)  +  Hugging Face spot files (NIFTY/BANKNIFTY/SENSEX)  +  synced 1m (Angel/Yahoo)
  5-minute  synced from Yahoo (last 60 days)       — fills days that have no 1-minute data, for 5m+ charts
  60-minute synced from Yahoo (last ~2 years)      — fills days without finer data, for 1h and 1D charts
  daily     synced from Yahoo (full history)       — fills days without intraday data, for 1D charts
All timestamps are naive IST. Intraday timeframes are resampled inside each session (anchored at 09:15, like
TradingView's NSE charts), so no bar ever spans two days.
"""
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, symbols

TIMEFRAMES = {"1": 1, "3": 3, "5": 5, "15": 15, "30": 30, "60": 60, "D": 0}
EXT_RES = ("1", "5", "60", "D")             # resolutions of synced data
COLS = ["open", "high", "low", "close", "volume"]
_EPOCH = pd.Timestamp("1970-01-01")
_lock = threading.Lock()
_cache: "OrderedDict[str, tuple]" = OrderedDict()
_tf_cache: "OrderedDict[tuple, pd.DataFrame]" = OrderedDict()
_CACHE_MAX = 4


def to_epoch(idx) -> np.ndarray:
    """Naive IST timestamps → integer seconds, treating IST wall time as UTC (the chart then shows IST labels)."""
    return ((pd.DatetimeIndex(idx) - _EPOCH) // pd.Timedelta("1s")).to_numpy(dtype=np.int64)


def from_epoch(sec) -> pd.Timestamp:
    return _EPOCH + pd.Timedelta(seconds=int(sec))


def empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLS, index=pd.DatetimeIndex([], name="date"), dtype="float64")


NSE = (config.SESSION_START, config.SESSION_END)


def sess(sym: str) -> tuple:
    """(start, end) minutes after 00:00 IST for this symbol; 24-hour markets span 1440 minutes (05:30 → 05:30)."""
    try:
        s = symbols.get(sym)
        return (s.session_start, s.session_end)
    except KeyError:
        return NSE


def _session(df: pd.DataFrame, se: tuple = NSE) -> pd.DataFrame:
    if se[1] - se[0] >= 1440:                       # 24-hour market
        return df
    m = df.index.hour * 60 + df.index.minute
    return df[(m >= se[0]) & (m < se[1])]


def _clean(df: pd.DataFrame, daily: bool = False, se: tuple = NSE) -> pd.DataFrame:
    if df is None or df.empty:
        return empty()
    if getattr(df.index, "tz", None) is not None:
        df = df.tz_convert("Asia/Kolkata").tz_localize(None) if hasattr(df, "tz_convert") else df
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.copy()
    for c in COLS:
        if c not in df:
            df[c] = 0.0
    df = df[COLS].astype("float64").dropna(subset=["open", "high", "low", "close"])
    df.index.name = "date"
    if daily:
        df.index = df.index.normalize()
        return df[~df.index.duplicated(keep="last")]
    return _session(df, se)


def _base_path(sym: str) -> Path:
    return config.DATA_DIR / "cache" / "base" / f"{sym}.parquet"


def ext_path(sym: str, res: str = "1") -> Path:
    return config.DATA_DIR / "cache" / "ext" / f"{sym}_{res}m.parquet"


def _hf_path(s) -> Path | None:
    p = config.HF_DIR / "index" / f"{s.hf}.parquet" if s.hf else None
    return p if p is not None and p.exists() else None


def _read_hf(p: Path) -> pd.DataFrame:
    d = pd.read_parquet(p)
    ts = pd.to_datetime(d["timestamp"])
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    d = d.assign(date=ts.dt.floor("min")).set_index("date")
    return _clean(d)


def _binance_files(s) -> list:
    d = config.DATA_DIR / "binance" / s.binance if s.binance else None
    return sorted(d.glob("*.zip")) if d is not None and d.exists() else []


def _read_binance(p: Path) -> pd.DataFrame:
    """Binance archive kline CSV (no header): open time in ms (µs from 2025), OHLC, base-asset volume. UTC → IST."""
    d = pd.read_csv(p, header=None, usecols=range(6), names=["t", "open", "high", "low", "close", "volume"])
    d = d[pd.to_numeric(d.t, errors="coerce").notna()]
    t = d.t.astype("int64")
    t = t.where(t < 10**14, t // 1000)
    d.index = pd.to_datetime(t, unit="ms") + pd.Timedelta(hours=5, minutes=30)
    d.index.name = "date"
    return d.drop(columns="t").astype("float64")


def _angel_files(s) -> list:
    d = symbols.mcx_dir(s.symbol) if s.exchange == "MCX" else None
    return sorted(d.glob("1m_*.parquet")) if d is not None and d.exists() else []


def load_base(sym: str) -> pd.DataFrame:
    """Kaggle 1-minute history (+ Hugging Face 1-minute spot where Kaggle has nothing; Binance archives for crypto;
    Angel One yearly downloads for MCX), cached as parquet."""
    s = symbols.get(sym)
    p = _base_path(sym)
    sources = ([Path(f) for f in (s.file,) if f] + [x for x in (_hf_path(s),) if x is not None] + _binance_files(s)
               + _angel_files(s))
    if p.exists() and all(p.stat().st_mtime >= f.stat().st_mtime for f in sources):
        return pd.read_parquet(p)
    parts = []
    if s.file:
        parts.append(_clean(pd.read_csv(s.file, parse_dates=["date"]).set_index("date")))
    hp = _hf_path(s)
    if hp is not None:
        hf = _read_hf(hp)
        if parts and len(parts[0]):
            have = set(parts[0].index.normalize().unique())
            hf = hf[~hf.index.normalize().isin(have)]          # Kaggle wins for days it covers
        parts.append(hf)
    if s.binance:
        parts += [_read_binance(f) for f in _binance_files(s)]
    parts += [pd.read_parquet(f) for f in _angel_files(s)]
    df = _clean(pd.concat(parts), se=sess(sym)) if parts else empty()
    if len(df):
        df.to_parquet(p)
    return df


def load_ext(sym: str, res: str = "1") -> pd.DataFrame:
    p = ext_path(sym, res)
    return pd.read_parquet(p) if p.exists() else empty()


def save_ext(sym: str, new: pd.DataFrame, res: str = "1") -> int:
    """Merge newly fetched bars into the synced cache (new data wins on overlap). Returns rows received."""
    if new is None or new.empty:
        return 0
    se = sess(sym)
    new = _clean(new, daily=res == "D", se=se)
    with _lock:
        merged = _clean(pd.concat([load_ext(sym, res), new]), daily=res == "D", se=se)
        merged.to_parquet(ext_path(sym, res))
        _cache.pop(sym, None)
    return len(new)


def _ext_mtime(sym: str) -> float:
    """Cache key: changes whenever synced data (or downloaded MCX history) changes."""
    m = sum(ext_path(sym, r).stat().st_mtime for r in EXT_RES if ext_path(sym, r).exists())
    s = symbols.all_symbols().get(sym)
    return m + (sum(f.stat().st_mtime for f in _angel_files(s)) if s is not None and s.exchange == "MCX" else 0)


def minute_bars(sym: str) -> pd.DataFrame:
    """All 1-minute bars: history + synced (synced data wins where both exist)."""
    key_mtime = _ext_mtime(sym)
    with _lock:
        hit = _cache.get(sym)
        if hit and hit[0] == key_mtime:
            _cache.move_to_end(sym)
            return hit[1]
    base, ext = load_base(sym), load_ext(sym, "1")
    df = _clean(pd.concat([base, ext]), se=sess(sym)) if len(ext) else base
    with _lock:
        _cache[sym] = (key_mtime, df)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return df


def bucket_key(idx: pd.DatetimeIndex, tf: str, anchor: int = config.SESSION_START) -> pd.DatetimeIndex:
    """Bars are counted from the session start (`anchor`, minutes after 00:00 IST). A day runs anchor → anchor + 24h
    and is labelled with the date it starts on (NSE: 09:15 → same date; 24-hour markets: 05:30 IST = 00:00 UTC)."""
    sh = idx - pd.Timedelta(minutes=anchor)
    day = sh.normalize()
    if tf == "D":
        return day
    n = TIMEFRAMES[tf]
    mins = sh.hour * 60 + sh.minute
    return day + pd.to_timedelta((mins // n) * n + anchor, unit="min")


def resample(df: pd.DataFrame, tf: str, anchor: int = config.SESSION_START) -> pd.DataFrame:
    """Session-anchored resampling of 1-minute (or coarser, aligned) bars (anchor = session start, minutes)."""
    if df.empty or tf == "1":
        return df
    g = df.groupby(bucket_key(df.index, tf, anchor))
    out = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(),
                        "close": g.close.last(), "volume": g.volume.sum()})
    out.index.name = "date"
    return out


def bars(sym: str, tf: str) -> pd.DataFrame:
    """Cached wrapper around _bars (invalidated when synced data changes)."""
    if tf not in TIMEFRAMES:
        raise ValueError(f"unsupported timeframe {tf!r}")
    key = (sym, tf, _ext_mtime(sym))
    with _lock:
        if key in _tf_cache:
            _tf_cache.move_to_end(key)
            return _tf_cache[key]
    out = _bars(sym, tf)
    with _lock:
        _tf_cache[key] = out
        while len(_tf_cache) > 12:
            _tf_cache.popitem(last=False)
    return out


def _bars(sym: str, tf: str) -> pd.DataFrame:
    """Finest data first; coarser synced data only fills days that have nothing finer, and only when the
    timeframe is a multiple of that resolution (5m data never feeds a 3m chart; hourly only feeds 1h / 1D)."""
    anchor = sess(sym)[0]
    out = resample(minute_bars(sym), tf, anchor)
    n = TIMEFRAMES[tf]
    for res in ("5", "60", "D"):
        usable = (tf == "D") or (res != "D" and n % int(res) == 0)
        if not usable:
            continue
        e = load_ext(sym, res)
        if e.empty:
            continue
        # compare trading days (a 24-hour market's day runs 05:30 → 05:30 IST), not calendar dates
        have = set(out.index if tf == "D" else bucket_key(out.index, "D", anchor)) if len(out) else set()
        ekey = e.index if res == "D" else bucket_key(e.index, "D", anchor)
        extra = e[~ekey.isin(have)]
        if len(extra):
            add = extra if (res == "D") else resample(extra, tf, anchor)
            out = pd.concat([out, add]).sort_index()
    return out[~out.index.duplicated(keep="first")]


def window(df: pd.DataFrame, start=None, end=None, limit=None, before=None) -> pd.DataFrame:
    """Slice by epoch seconds. `before` = load older history (bars strictly before that time)."""
    if start is not None:
        df = df[df.index >= from_epoch(start)]
    if end is not None:
        df = df[df.index <= from_epoch(end)]
    if before is not None:
        df = df[df.index < from_epoch(before)]
    if limit:
        df = df.iloc[-int(limit):] if start is None else df.iloc[: int(limit)]
    return df


def records(df: pd.DataFrame) -> list:
    t = to_epoch(df.index)
    o, h, l, c, v = (df[k].to_numpy() for k in COLS)
    r2 = lambda x: round(float(x), 2)
    return [{"time": int(t[i]), "open": r2(o[i]), "high": r2(h[i]), "low": r2(l[i]),
             "close": r2(c[i]), "volume": float(v[i])} for i in range(len(df))]


def coverage(sym: str) -> dict:
    span = lambda d: None if d.empty else [str(d.index.min()), str(d.index.max())]
    return {"history_1m": span(load_base(sym)), **{f"synced_{r}": span(load_ext(sym, r)) for r in EXT_RES}}
