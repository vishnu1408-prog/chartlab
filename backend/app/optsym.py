"""Option contracts as chart symbols: "NIFTY 27-Oct-26 23100 CE", "CRUDEOIL 17-Nov-26 9250 PE", "RELIANCE 27-Oct-26 1210 CE".

A name is parsed into (root, expiry, strike, CE/PE) and turned into a Symbol on the fly (kind "option"), so the normal
chart, indicators, drawings, replay and live feed work on the option's premium:
  history  — the saved 1-minute option files (data/hf/options/<ROOT>/<expiry>.parquet: Hugging Face + ChartLab's
             Angel recordings) for index and MCX options
  recent   — Angel 1-minute candles of the live contract (≤ 60 days back), on chart open / sync
  live     — Angel quotes + candles while the market is open (same feed as the underlyings)
Expired contracts that are not on file have no data (Angel only serves live contracts).
"""
from __future__ import annotations

import datetime as dt
import re
from functools import lru_cache

import pandas as pd

from . import config

NAME_RE = re.compile(r"^([A-Z0-9&\-]+) (\d{2}-[A-Za-z]{3}-\d{2}) (\d+(?:\.\d+)?) (CE|PE)$")
SHORT_RE = re.compile(r"^([A-Z0-9&\- ]+?) (\d+(?:\.\d+)?) ?(CE|PE)$")
ROOT_TO_CHART = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE", "SENSEX": "SENSEX",
                 "CRUDEOIL": "MCX CRUDEOIL", "NATURALGAS": "MCX NATURALGAS", "GOLD": "MCX GOLD", "SILVER": "MCX SILVER"}
CHART_TO_ROOT = {v: k for k, v in ROOT_TO_CHART.items()}


def name(root: str, expiry: dt.date | str, strike: float, typ: str) -> str:
    e = dt.date.fromisoformat(expiry) if isinstance(expiry, str) else expiry
    k = f"{float(strike):g}"
    return f"{root} {e:%d-%b-%y} {k} {typ}"


def parse(sym: str) -> dict | None:
    m = NAME_RE.match(sym or "")
    if not m:
        return None
    root, e, k, typ = m.groups()
    try:
        exp = dt.datetime.strptime(e.title(), "%d-%b-%y").date()
    except ValueError:
        return None
    underlying = ROOT_TO_CHART.get(root, root)              # a stock's chart symbol is its own name
    seg = "MCX" if underlying.startswith("MCX ") else "BFO" if root == "SENSEX" else "NFO"
    return {"root": root, "expiry": exp, "strike": float(k), "type": typ, "underlying": underlying, "seg": seg}


def contract(sym: str) -> dict | None:
    """The live Angel contract of an option symbol (None if it is not trading any more)."""
    p = parse(sym)
    if p is None or config.OFFLINE:
        return None
    try:
        from . import paper
        _, _, rows = paper.contracts(p["underlying"])
    except Exception:
        return None
    for e, k, t, tok, tsym, lot in rows:
        if e == p["expiry"] and abs(k - p["strike"]) < 1e-6 and t == p["type"]:
            return {"token": str(tok), "tsym": tsym, "lot": int(lot)}
    return None


@lru_cache(maxsize=256)
def symbol(sym: str):
    """A Symbol for an option name (cached per name)."""
    from .symbols import Symbol
    p = parse(sym)
    if p is None:
        return None
    mcx = p["seg"] == "MCX"
    c = contract(sym) or {}
    return Symbol(sym, "option", "", "", c.get("tsym", ""), c.get("lot", 1), p["seg"],
                  session_start=540 if mcx else config.SESSION_START, session_end=1440 if mcx else config.SESSION_END,
                  square_off=23 * 60 if mcx else 0,
                  note=f"{p['type']} option on {p['underlying']} · strike {p['strike']:g} · expiry {p['expiry']:%d %b %Y}"
                       + ("" if c else " · not trading any more (history from saved files only)"))


def token(sym: str) -> str | None:
    c = contract(sym)
    return c["token"] if c else None


def history(sym: str) -> pd.DataFrame:
    """Saved 1-minute premiums for this contract (index / MCX options), IST naive index."""
    p = parse(sym)
    if p is None or p["root"] not in ROOT_TO_CHART:
        return pd.DataFrame()
    from . import options
    ch = options.load_chain(p["root"], p["expiry"])
    if ch is None or ch.empty:
        return pd.DataFrame()
    c = ch[(ch.strike == p["strike"]) & (ch.option_type == p["type"])]
    if c.empty:
        return pd.DataFrame()
    d = c.drop_duplicates("date", keep="last").set_index("date").sort_index()[["open", "high", "low", "close"]]
    d["volume"] = 0.0
    d.index.name = "date"
    return d


def resolve(q: str) -> str | None:
    """Text from the symbol box → an option symbol name. Accepts the full name in any case
    ("nifty 27-oct-26 23100 ce") or a short one ("NIFTY 23100 CE", "RELIANCE 1210 PE", "CRUDEOIL 9250 CE",
    "NIFTY 50 23100 CE") → the nearest expiry that lists that strike."""
    q = " ".join((q or "").upper().split())
    p = parse(q)
    if p:
        return name(p["root"], p["expiry"], p["strike"], p["type"])
    m = SHORT_RE.match(q)
    if not m:
        return None
    root, k, typ = m.group(1).strip(), float(m.group(2)), m.group(3)
    root = CHART_TO_ROOT.get(root, root)
    underlying = ROOT_TO_CHART.get(root, root)
    try:
        from . import paper
        _, _, rows = paper.contracts(underlying)
    except Exception:
        return None
    today = dt.date.today()
    exps = sorted({e for e, kk, t, *_ in rows if e >= today and abs(kk - k) < 1e-6 and t == typ})
    return name(root, exps[0], k, typ) if exps else None
