"""ATM option prices for practice trading (index options: NIFTY, BANKNIFTY, SENSEX).

Real prices: Hugging Face dataset thetrademarkk/india-index-options-1m (CC BY-NC 4.0, educational use) — one parquet
per expiry with 1-minute OHLC per strike; downloaded on first use and cached in data/hf/options/<SYM>/<expiry>.parquet.
Coverage is dense for roughly the last 8 trading days before each expiry (2021-05 → 2026), sparse earlier.
Model prices (fallback when no real bar is near the entry): Black-Scholes with India VIX (previous close) as volatility
(×1.2 for BANKNIFTY), real time to the 15:30 expiry close in trading days → theta and gamma are included.
"""
import datetime as dt
import json
import logging
import math
import threading
import time
from collections import OrderedDict

import numpy as np
import pandas as pd

from . import config, store, symbols

log = logging.getLogger("chartlab.options")
HF_REPO = "thetrademarkk/india-index-options-1m"
STEP = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100}
VOL_MULT = {"NIFTY": 1.0, "BANKNIFTY": 1.2, "SENSEX": 1.0}
_lock = threading.Lock()
_chains: "OrderedDict[tuple, pd.DataFrame]" = OrderedDict()


# ───────────────────────────── expiries ─────────────────────────────
def _rule_expiries(hf: str, start: dt.date, end: dt.date) -> list:
    """Approximate expiry calendar outside the dataset (holidays ignored).
    NIFTY: monthly last Thursday before Feb 2019, weekly Thursday after, weekly Tuesday from Sep 2025.
    BANKNIFTY: weekly Thursday until Sep 2023, Wednesday until Nov 2024, then monthly last Thursday / Tuesday (Sep 2025).
    SENSEX: weekly Friday until Dec 2024, Tuesday until Aug 2025, Thursday after."""
    out, d = [], start
    while d <= end + dt.timedelta(days=40):
        wd = d.weekday()
        last_in_month = (d + dt.timedelta(days=7)).month != d.month
        if hf == "NIFTY":
            ok = (wd == 3 and last_in_month) if d < dt.date(2019, 2, 11) else (wd == 3 if d < dt.date(2025, 9, 1) else wd == 1)
        elif hf == "BANKNIFTY":
            if d < dt.date(2023, 9, 4):
                ok = wd == 3
            elif d < dt.date(2024, 11, 20):
                ok = wd == 2
            else:
                ok = last_in_month and wd == (3 if d < dt.date(2025, 9, 1) else 1)
        else:
            ok = wd == (4 if d < dt.date(2025, 1, 1) else 1 if d < dt.date(2025, 9, 1) else 3)
        if ok:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def dataset_expiries(hf: str) -> list:
    """Expiry dates that have a file in the dataset (cached for a week)."""
    p = config.HF_DIR / f"expiries_{hf}.json"
    if p.exists() and (config.OFFLINE or time.time() - p.stat().st_mtime < 7 * 86400):
        return [dt.date.fromisoformat(x) for x in json.loads(p.read_text())]
    if config.OFFLINE:
        local = sorted((config.HF_DIR / "options" / hf).glob("*.parquet")) if (config.HF_DIR / "options" / hf).exists() else []
        return [dt.date.fromisoformat(f.stem) for f in local]
    try:
        import requests
        r = requests.get(f"https://huggingface.co/api/datasets/{HF_REPO}/tree/main/options/{hf}", timeout=30)
        r.raise_for_status()
        days = sorted(x["path"].split("/")[-1][:-8] for x in r.json() if x["path"].endswith(".parquet"))
        p.write_text(json.dumps(days))
        return [dt.date.fromisoformat(x) for x in days]
    except Exception as e:
        log.warning("expiry list %s: %s", hf, e)
        return [dt.date.fromisoformat(x) for x in json.loads(p.read_text())] if p.exists() else []


def expiry_for(hf: str, t: pd.Timestamp) -> tuple:
    """Nearest expiry on/after the trade day (after 15:30 on expiry day → the next one). Returns (date, from_dataset)."""
    day = t.date() if (t.hour * 60 + t.minute) < config.SESSION_END else t.date() + dt.timedelta(days=1)
    ds = [d for d in dataset_expiries(hf) if d >= day]
    if ds and (ds[0] - day).days <= 35:
        return ds[0], True
    rule = [d for d in _rule_expiries(hf, day, day) if d >= day]
    return rule[0], False


# ───────────────────────────── pricing ─────────────────────────────
def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, sig, call):
    T = max(T, 1e-6)
    sig = max(sig, 1e-4)
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return S * _ncdf(d1) - K * _ncdf(d2) if call else K * _ncdf(-d2) - S * _ncdf(-d1)


def years_to_expiry(t: pd.Timestamp, expiry: dt.date) -> float:
    """Trading time (252-day year) from t to 15:30 on the expiry day; weekends skipped, holidays ignored."""
    d = t.normalize()
    days = 0
    cur = d.date()
    while cur < expiry:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    today_left = max(0.0, (config.SESSION_END - (t.hour * 60 + t.minute)) / 375)
    return (days + today_left) / 252


def vix_prev_close(t: pd.Timestamp) -> float:
    try:
        v = store.bars("INDIA VIX", "D")
        prev = v[v.index < t.normalize()]
        if len(prev):
            return float(prev.close.iloc[-1])
    except KeyError:
        pass
    return 15.0


def model_bars(sym: str, hf: str, strike: float, call: bool, expiry: dt.date, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    u = store.minute_bars(sym)
    u = u[(u.index >= start) & (u.index <= end)]
    if u.empty:
        return u
    sig = vix_prev_close(start) / 100 * VOL_MULT.get(hf, 1.0)
    rows = []
    for ts, b in u.iterrows():
        T = years_to_expiry(ts + pd.Timedelta(minutes=1), expiry)
        p = {k: bs(b[k], strike, T, sig, call) for k in ("open", "high", "low", "close")}
        hi, lo = (p["high"], p["low"]) if call else (p["low"], p["high"])
        rows.append((ts, p["open"], max(hi, p["open"], p["close"]), min(lo, p["open"], p["close"]), p["close"]))
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"]).set_index("date")


# ───────────────────────────── real option data ─────────────────────────────
def chain_path(hf: str, expiry: dt.date):
    return config.HF_DIR / "options" / hf / f"{expiry.isoformat()}.parquet"


def load_chain(hf: str, expiry: dt.date) -> pd.DataFrame | None:
    key = (hf, expiry)
    with _lock:
        if key in _chains:
            _chains.move_to_end(key)
            return _chains[key]
    p = chain_path(hf, expiry)
    if not p.exists() and not config.OFFLINE:
        try:
            import requests
            p.parent.mkdir(parents=True, exist_ok=True)
            r = requests.get(f"https://huggingface.co/datasets/{HF_REPO}/resolve/main/options/{hf}/{expiry.isoformat()}.parquet", timeout=120)
            if r.status_code == 200:
                tmp = p.with_suffix(".part")
                tmp.write_bytes(r.content)
                tmp.rename(p)
        except Exception as e:
            log.warning("option chain %s %s: %s", hf, expiry, e)
    if not p.exists():
        return None
    d = pd.read_parquet(p, columns=["timestamp", "open", "high", "low", "close", "strike", "option_type"])
    ts = pd.to_datetime(d["timestamp"])
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    d = d.assign(date=ts.dt.floor("min")).drop(columns="timestamp")
    with _lock:
        _chains[key] = d
        while len(_chains) > 6:
            _chains.popitem(last=False)
    return d


def contract(symbol: str, time_s: int, opt_type: str, spot: float | None = None, strike: float | None = None,
             max_stale_min: int = 5) -> dict:
    """ATM (or given strike) option for a practice trade at `time_s`: premium bars from that minute to the end of the
    day. Real dataset prices when the contract traded within `max_stale_min` minutes of the entry, else model prices."""
    s = symbols.get(symbol)
    if not s.options or not s.hf:
        raise ValueError(f"option practice is available for NIFTY 50, NIFTY BANK and SENSEX, not {symbol}")
    call = opt_type.upper() == "CE"
    t = store.from_epoch(time_s)
    if spot is None:
        u = store.minute_bars(symbol)
        u = u[u.index <= t]
        if u.empty:
            raise ValueError("no underlying price at that time")
        spot = float(u.close.iloc[-1])
    step = STEP.get(s.hf, 50)
    k = float(strike) if strike else round(spot / step) * step
    expiry, in_ds = expiry_for(s.hf, t)
    day_end = t.normalize() + pd.Timedelta(minutes=config.SESSION_END - 1)
    out = {"symbol": symbol, "type": "CE" if call else "PE", "strike": k, "expiry": expiry.isoformat(),
           "spot": spot, "source": "model", "note": ""}
    bars = None
    if in_ds:
        chain = load_chain(s.hf, expiry)
        if chain is not None:
            c = chain[(chain.strike == k) & (chain.option_type == out["type"])].set_index("date").sort_index()
            c = c[~c.index.duplicated(keep="last")]
            before = c[(c.index <= t) & (c.index >= t - pd.Timedelta(minutes=max_stale_min))]
            if len(before):
                day = c[(c.index >= before.index[-1]) & (c.index <= day_end)][["open", "high", "low", "close"]]
                # fill minutes without a trade with the last price so the position can always be marked
                full = pd.date_range(before.index[-1], day_end, freq="1min")
                mins = full[(full.hour * 60 + full.minute >= config.SESSION_START)]
                day = day.reindex(mins)
                last = day.close.ffill()
                day = day.assign(close=last, open=day.open.fillna(last), high=day.high.fillna(last), low=day.low.fillna(last))
                bars = day[day.index >= before.index[-1]]
                out["source"] = "real"
                out["coverage"] = round(float(c[(c.index >= t) & (c.index <= day_end)].shape[0] / max(1, len(mins))), 2)
            else:
                out["note"] = "no traded price for this strike near the entry — using the model"
        else:
            out["note"] = "option file not available — using the model"
    if bars is None:
        bars = model_bars(symbol, s.hf, k, call, expiry, t.floor("min"), day_end)
        out["vix"] = vix_prev_close(t)
    out["bars"] = store.records(bars.assign(volume=0.0)) if len(bars) else []
    if not out["bars"]:
        raise ValueError("no option price available at that time")
    out["premium"] = out["bars"][0]["close"]
    return out
