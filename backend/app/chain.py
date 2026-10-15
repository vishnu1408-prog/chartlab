"""Live option chain from Angel One market quotes (data only — never places orders).

NIFTY / BANKNIFTY / FINNIFTY (NFO), SENSEX (BFO) and MCX options on futures (gold, silver, crude oil, natural gas).
One quote request per refresh: the strikes around the money (CE + PE) plus the underlying (the index, or for MCX
the futures contract the options are written on). OI and volume are shown in lots (Angel sends OI in units on every
exchange, volume in units on NSE / BSE but already in lots on MCX — checked against the futures contracts).

ΔOI: Angel's quote has no previous-day OI, so the change is measured from the first time ChartLab saw that strike
today (`oi_since` says when). IV is solved from the option's last price with Black's formula (zero rate on the
forward; forward = spot for indices) — an estimate, good to compare strikes and days, not to the decimal.
"""
import datetime as dt
import math
import threading
import time

from . import sources, symbols
from .options import bs

ROOT = {"NIFTY 50": ("NFO", "NIFTY"), "NIFTY BANK": ("NFO", "BANKNIFTY"), "NIFTY FIN SERVICE": ("NFO", "FINNIFTY"),
        "SENSEX": ("BFO", "SENSEX"), "MCX GOLD": ("MCX", "GOLD"), "MCX SILVER": ("MCX", "SILVER"),
        "MCX CRUDEOIL": ("MCX", "CRUDEOIL"), "MCX NATURALGAS": ("MCX", "NATURALGAS")}
CLOSE_MIN = {"NFO": 15 * 60 + 30, "BFO": 15 * 60 + 30, "MCX": 23 * 60 + 30}     # options stop trading on expiry day

_lock = threading.Lock()
_cache: dict = {}          # (symbol, expiry, n) → (time, result)
_oi0: dict = {}            # (date, token) → (first OI seen today, when)


def supported(symbol: str) -> bool:
    return symbol in ROOT


def years_left(now: dt.datetime, expiry: dt.date, seg: str) -> float:
    end = dt.datetime.combine(expiry, dt.time()) + dt.timedelta(minutes=CLOSE_MIN[seg])
    return max((end - now).total_seconds(), 60) / (365 * 86400)


def implied_vol(price, S, K, T, call):
    """Black (zero rate) implied volatility by bisection; None below intrinsic value or for missing prices."""
    if not price or price <= 0 or not S or S <= 0:
        return None
    intrinsic = max(S - K, 0) if call else max(K - S, 0)
    if price <= intrinsic + 1e-9:
        return None
    lo, hi = 0.005, 5.0
    if bs(S, K, T, hi, call) < price:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs(S, K, T, mid, call) < price:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2 * 100, 1)


def contracts(symbol: str):
    seg, name = ROOT[symbol]
    sources.Angel.token(symbol)                      # loads / refreshes the scrip master once a day
    return seg, name, sources.Angel._options.get((seg, name), [])


def underlying(symbol: str, seg: str, name: str, expiry: dt.date):
    """(exchange, token, label): the index, or the MCX futures contract the options settle into."""
    if seg == "MCX":
        futs = [f for f in sources.Angel._futures.get(name, []) if f[0] >= expiry]
        return ("MCX", futs[0][1], futs[0][2]) if futs else (None, None, None)
    s = symbols.get(symbol)
    return (s.exchange, sources.Angel.token(symbol), symbol)


def pick_strikes(strikes: list, spot: float, n: int) -> list:
    """the n strikes below and above the one nearest the spot (2n + 1 in all)"""
    ks = sorted(set(strikes))
    if not ks:
        return []
    i = min(range(len(ks)), key=lambda j: abs(ks[j] - spot))
    return ks[max(0, i - n): i + n + 1]


def _side(q: dict | None, lot: int, key, today, now, S, K, T, call, vol_lot: int):
    if not q:
        return None
    oi = (q.get("opnInterest") or 0) / lot
    with _lock:
        first = _oi0.setdefault((today, key), (oi, now.strftime("%H:%M")))
    depth = q.get("depth") or {}
    bid = (depth.get("buy") or [{}])[0].get("price") or None
    ask = (depth.get("sell") or [{}])[0].get("price") or None
    ltp = q.get("ltp")
    return {"ltp": ltp, "chg": q.get("netChange"), "oi": round(oi), "doi": round(oi - first[0]),
            "vol": round((q.get("tradeVolume") or 0) / vol_lot), "bid": bid, "ask": ask,
            "iv": implied_vol(ltp, S, K, T, call), "since": first[1]}


def build(symbol: str, expiry: dt.date, rows: list, quotes: dict, spot: float, spot_q: dict | None,
          now: dt.datetime, seg: str, under_label: str, n: int, expiries: list) -> dict:
    """Pure: contracts + quotes → the chain table (unit-tested without the network)."""
    T = years_left(now, expiry, seg)
    today = now.date()
    by = {(k, t): (tok, sym, lot) for e, k, t, tok, sym, lot in rows if e == expiry}
    lot = next((lot for e, k, t, tok, sym, lot in rows if e == expiry), 1)
    vol_lot = 1 if seg == "MCX" else lot           # Angel: MCX volume is already in lots; OI is in units everywhere
    out, ce_oi, pe_oi = [], 0, 0
    for K in pick_strikes([k for (k, t) in by], spot, n):
        ce, pe = by.get((K, "CE")), by.get((K, "PE"))
        c = _side(quotes.get(ce[0]) if ce else None, lot, ce[0] if ce else None, today, now, spot, K, T, True, vol_lot)
        p = _side(quotes.get(pe[0]) if pe else None, lot, pe[0] if pe else None, today, now, spot, K, T, False, vol_lot)
        ce_oi += (c or {}).get("oi", 0)
        pe_oi += (p or {}).get("oi", 0)
        out.append({"strike": K, "ce": c, "pe": p, "ce_symbol": ce[1] if ce else None, "pe_symbol": pe[1] if pe else None})
    atm = min(out, key=lambda r: abs(r["strike"] - spot)) if out else None
    straddle = (atm["ce"]["ltp"] + atm["pe"]["ltp"]) if atm and atm["ce"] and atm["pe"] and atm["ce"]["ltp"] and atm["pe"]["ltp"] else None
    since = min((r[s]["since"] for r in out for s in ("ce", "pe") if r[s]), default=None)
    return {"symbol": symbol, "expiry": expiry.isoformat(), "expiries": [e.isoformat() for e in expiries],
            "underlying": {"label": under_label, "price": spot, "chg": (spot_q or {}).get("netChange")},
            "lot": lot, "atm": atm["strike"] if atm else None, "straddle": round(straddle, 2) if straddle else None,
            "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None, "oi_since": since, "days_left": round(T * 365, 1),
            "rows": out, "at": now.strftime("%H:%M:%S")}


def chain(symbol: str, expiry: str | None = None, n: int = 10, now: dt.datetime | None = None) -> dict:
    from .sync import now_ist
    if not supported(symbol):
        raise KeyError(f"no option chain for {symbol}")
    if not sources.Angel.available():
        return {"symbol": symbol, "error": "Live option chains need Angel One (fill ~/.config/angel/creds.env)."}
    n = max(2, min(int(n), 12))                      # 2n + 1 strikes × 2 + underlying ≤ 50 tokens = one request
    seg, name, rows = contracts(symbol)
    expiries = sorted({r[0] for r in rows})
    if not expiries:
        return {"symbol": symbol, "error": f"Angel's contract list has no live {name} options."}
    exp = dt.date.fromisoformat(expiry) if expiry and dt.date.fromisoformat(expiry) in expiries else expiries[0]
    key = (symbol, exp, n)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < 3:
            return hit[1]
    ux, utok, ulabel = underlying(symbol, seg, name, exp)
    if utok is None:
        return {"symbol": symbol, "error": "underlying not found in Angel's contract list"}
    uq = sources.Angel.quotes({ux: [utok]}, "LTP").get(str(utok))
    spot = (uq or {}).get("ltp")
    if not spot:
        return {"symbol": symbol, "error": f"no live price for {ulabel} ({sources.Angel._last_error or 'Angel'})"}
    mine = [r for r in rows if r[0] == exp]
    ks = set(pick_strikes([r[1] for r in mine], spot, n))
    toks = [r[3] for r in mine if r[1] in ks]
    want = {seg: list(toks)}
    want.setdefault(ux, []).append(utok)             # MCX: options and their futures are on the same exchange
    q = sources.Angel.quotes(want, "FULL")
    uq = q.get(str(utok)) or uq
    spot = (uq or {}).get("ltp") or spot
    res = build(symbol, exp, mine, q, spot, uq, now or now_ist(), seg, ulabel, n, expiries)
    with _lock:
        _cache[key] = (time.time(), res)
    return res
