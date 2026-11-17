"""Live paper trading in NIFTY (NFO) and SENSEX (BFO) index options — real Angel One quotes, no real orders.

Accounts, each with its own cash, positions, orders and history (SQLite tables paper_* in data/chartlab.db):
  manual — the user's own trades from the order ticket.
  bot    — the Morning Edge signal traded automatically on real option prices: at the 09:45 decision buy the
           ITM-200 CE (BUY signal) / PE (SELL signal) of the nearest expiry, BOT_LOTS lots; stop when the INDEX moves
           35 points against; at +35 index points sell half and move the rest's stop to the entry index level; out at
           15:15 (the tested exit "E2").
  crypto — BTC options on Delta Exchange India (public market data, no login), account in USD. 1 contract =
           0.001 BTC; prices are quoted per 1 BTC like on Delta's screen. Trades 24/7; to stay intraday every BTC
           position is sold at the end of the crypto day (05:30 IST = 00:00 UTC) and 5 minutes before its contract
           settles (daily expiries settle 17:30 IST). Fee: 0.01 % of the underlying value, capped at 3.5 % of the
           premium, + 18 % GST (editable).
Option buying only (long CE / PE): BUY opens or adds, SELL reduces or closes. Market orders fill at the best ask
(buy) / best bid (sell) of the live quote (last price if the depth is empty); limit orders fill when the quote
crosses the limit. Stops and targets can be on the OPTION price or on the INDEX level. Everything still open is
sold at the square-off time (default 15:15). Charges per order: brokerage, STT on sells, exchange transaction
charges, SEBI fee, stamp duty on buys, GST — editable in the settings (rates change; check your broker's sheet).
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import threading
import time
import uuid

from . import config

log = logging.getLogger("chartlab.paper")
DIR = config.DATA_DIR / "paper"
UNDER = {"NIFTY 50": ("NFO", "NIFTY", "NSE"), "SENSEX": ("BFO", "SENSEX", "BSE"), "BTC": ("DELTA", "BTC", None),
         "MCX CRUDEOIL": ("MCX", "CRUDEOIL", None), "MCX NATURALGAS": ("MCX", "NATURALGAS", None)}
STEP = {"NIFTY 50": 50, "SENSEX": 100, "BTC": 200, "MCX CRUDEOIL": 50, "MCX NATURALGAS": 5}
ACCOUNTS = ("manual", "bot", "gann", "plan", "ng", "crypto")             # bot = Morning Edge; gann / plan: bots.py
DELTA = "https://api.india.delta.exchange/v2"
BTC_MULT = 0.001                                             # BTC per Delta option contract
BOT_LOTS = 2
DEFAULT_SETTINGS = {
    "capital": 150_000, "square_off": "15:15", "max_lots": 10, "auto_morning_edge": True, "bot_lots": BOT_LOTS,
    "me_target2": 0,  # Morning Edge lot 2: 2nd target in index points after lot 1's target (0 = off; tested worse)
    "charges": {"brokerage": 20.0, "stt_sell": 0.001, "exch_nfo": 0.0003503, "exch_bfo": 0.000325, "sebi": 0.000001,
                "stamp_buy": 0.00003, "gst": 0.18, "ctt_sell": 0.0005, "exch_mcx": 0.000418},
}
CRYPTO_SETTINGS = {
    "capital": 1_800, "currency": "USD", "usd_inr": 88.0, "square_off": "05:30 IST (end of the UTC day)",
    "max_lots": 500, "charges": {"fee_rate": 0.0001, "fee_cap": 0.035, "gst": 0.18},
}
_lock = threading.RLock()
status: dict = {"state": "idle", "quotes_at": None, "error": None}


# ───────────────────────────── storage ─────────────────────────────
def _defaults(account: str) -> dict:
    return CRYPTO_SETTINGS if account == "crypto" else DEFAULT_SETTINGS


def _new(account: str, settings: dict | None = None) -> dict:
    s = copy.deepcopy(settings or _defaults(account))
    from .sync import now_ist
    return {"account": account, "created": now_ist().isoformat(timespec="seconds"), "settings": s,
            "cash": float(s["capital"]), "positions": [], "orders": [], "trades": [], "events": []}


def load(account: str) -> dict:
    from . import db
    if account not in ACCOUNTS:
        raise KeyError(account)
    st = db.paper_load(account)
    if st is None:
        return _new(account)
    for k, v in _defaults(account).items():                     # settings added in later versions
        st["settings"].setdefault(k, copy.deepcopy(v))
    return st


def save(st: dict):
    from . import db
    db.paper_save(st)


def _event(st: dict, kind: str, text: str, now: dt.datetime):
    st["events"].append({"id": uuid.uuid4().hex[:10], "at": now.isoformat(timespec="seconds"), "kind": kind, "text": text})


# ───────────────────────────── pure money maths ─────────────────────────────
def charges(side: str, price: float, qty: int, seg: str, c: dict, spot: float | None = None, mult: float = 1.0) -> dict:
    """Charges of ONE executed option order in the account currency. side BUY / SELL, price = premium per unit
    (per BTC for Delta, where one contract = mult BTC)."""
    if seg == "DELTA":
        prem = price * qty * mult
        fee = min(c["fee_rate"] * spot * qty * mult, c["fee_cap"] * prem) if spot else c["fee_cap"] * prem
        gst = fee * c["gst"]
        return {"fee": round(fee, 4), "gst": round(gst, 4), "total": round(fee + gst, 4)}
    turnover = price * qty
    brok = min(c["brokerage"], 0.0025 * turnover) if turnover > 0 else 0.0     # flat ₹20 (tiny orders cheaper)
    exch = turnover * (c["exch_bfo"] if seg == "BFO" else c.get("exch_mcx", 0.000418) if seg == "MCX" else c["exch_nfo"])
    sebi = turnover * c["sebi"]
    sell_tax = c.get("ctt_sell", 0.0005) if seg == "MCX" else c["stt_sell"]       # MCX: CTT instead of STT
    stt = turnover * sell_tax if side == "SELL" else 0.0
    stamp = turnover * c["stamp_buy"] if side == "BUY" else 0.0
    gst = (brok + exch + sebi) * c["gst"]
    tot = brok + exch + sebi + stt + stamp + gst
    return {k: round(v, 2) for k, v in dict(brokerage=brok, exchange=exch, sebi=sebi, stt=stt, stamp=stamp, gst=gst,
                                              total=tot).items()}


def fill_price(side: str, q: dict | None) -> float | None:
    """Market order: buy at the best ask, sell at the best bid; last price if the book is empty."""
    if not q:
        return None
    px = q.get("ask") if side == "BUY" else q.get("bid")
    return float(px) if px else (float(q["ltp"]) if q.get("ltp") else None)


def level_hit(level: dict | None, kind: str, opt_type: str, q: dict | None, under: float | None) -> bool:
    """Has a stop / target been reached? level = {"basis": "option" | "index", "price": x}.
    Long CE gains when the index rises, long PE when it falls; the option itself is always long."""
    if not level or level.get("price") is None:
        return False
    p = float(level["price"])
    if level["basis"] == "option":
        if not q:
            return False
        px = q.get("bid") or q.get("ltp")                        # what we could sell at now
        if not px:
            return False
        return px <= p if kind == "stop" else px >= p
    if under is None:
        return False
    up = opt_type == "CE"
    if kind == "stop":
        return under <= p if up else under >= p
    return under >= p if up else under <= p


# ───────────────────────────── contracts & quotes ─────────────────────────────
_delta: dict = {"t": 0.0, "rows": []}


def delta_tickers(max_age: float = 2.0) -> list:
    """All live BTC option tickers from Delta Exchange India (one public request, cached a couple of seconds)."""
    import requests
    if time.time() - _delta["t"] > max_age:
        r = requests.get(f"{DELTA}/tickers", params={"contract_types": "call_options,put_options",
                                                     "underlying_asset_symbols": "BTC"}, timeout=15)
        r.raise_for_status()
        _delta.update(t=time.time(), rows=r.json().get("result") or [])
    return _delta["rows"]


def _delta_expiry(sym: str) -> dt.date:
    d = sym.rsplit("-", 1)[1]                                 # C-BTC-82000-171026 → 17 Oct 2026
    return dt.date(2000 + int(d[4:6]), int(d[2:4]), int(d[0:2]))


def settle_time(c: dict) -> dt.datetime | None:
    """IST settlement time of a Delta contract (12:00 UTC on the expiry day)."""
    if c.get("seg") != "DELTA":
        return None
    return dt.datetime.combine(dt.date.fromisoformat(c["expiry"]), dt.time(17, 30))


def stock_names() -> list:
    """NSE stocks that have live options (Angel scrip master, OPTSTK)."""
    from . import sources
    sources.Angel.token("NIFTY 50")                           # loads / refreshes the scrip master
    return sorted(n for (seg, n) in (sources.Angel._options or {}) if seg == "NFO" and n not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"))


def is_stock(symbol: str) -> bool:
    return symbol not in UNDER and symbol in set(stock_names())


OTHER_INDEX = ("NIFTY BANK", "NIFTY FIN SERVICE")          # index options shown on the OI page (not paper-traded)


def contracts(symbol: str) -> tuple[str, str, list]:
    if symbol in OTHER_INDEX:
        from . import chain
        return chain.contracts(symbol)
    if symbol not in UNDER:                                     # an F&O stock (e.g. RELIANCE)
        from . import sources
        sources.Angel.token("NIFTY 50")
        return "NFO", symbol, (sources.Angel._options or {}).get(("NFO", symbol), [])
    if symbol == "BTC":
        rows = [(_delta_expiry(x["symbol"]), float(x["strike_price"]), "CE" if x["contract_type"] == "call_options" else "PE",
                 x["symbol"], x["symbol"], 1) for x in delta_tickers(30) if x.get("strike_price")]
        return "DELTA", "BTC", rows
    from . import chain
    seg, name, rows = chain.contracts(symbol)
    return seg, name, rows


def find(symbol: str, expiry: str, strike: float, typ: str) -> dict | None:
    seg, name, rows = contracts(symbol)
    e = dt.date.fromisoformat(expiry)
    for ex, k, t, tok, tsym, lot in rows:
        if ex == e and abs(k - float(strike)) < 1e-6 and t == typ:
            return {"symbol": symbol, "seg": seg, "token": str(tok), "tsym": tsym, "lot": int(lot), "expiry": expiry,
                    "strike": float(k), "type": typ, "mult": BTC_MULT if seg == "DELTA" else 1.0}
    return None


def _under_tokens() -> dict:
    from . import sources
    out = {}
    for sym, (_, _, ex) in UNDER.items():
        if ex is None:
            continue
        tok = sources.Angel.token(sym)
        if tok:
            out[sym] = (ex, str(tok))
    return out


def quotes(contracts_: list, want: tuple = ()) -> tuple[dict, dict]:
    """Live quotes: ({token: {ltp, bid, ask}}, {underlying symbol: price}). Angel (one request, ≤ 50 tokens) for
    NSE / BSE contracts, Delta Exchange for BTC. `want`: underlyings needed even without contracts."""
    if config.OFFLINE:
        return {}, {}
    q, under = {}, {}
    if any(c["seg"] == "DELTA" for c in contracts_) or "BTC" in want:
        try:
            for x in delta_tickers():
                qq = x.get("quotes") or {}
                q[x["symbol"]] = {"ltp": float(x["mark_price"]) if x.get("mark_price") else None,
                                  "bid": float(qq["best_bid"]) if qq.get("best_bid") else None,
                                  "ask": float(qq["best_ask"]) if qq.get("best_ask") else None, "close": x.get("close")}
                if x.get("spot_price"):
                    under["BTC"] = float(x["spot_price"])
        except Exception as e:
            log.warning("delta quotes: %s", e)
            status["error"] = f"Delta Exchange: {e}"[:200]
    mcx = [c for c in contracts_ if c["seg"] == "MCX"]
    if mcx:
        qm, um = _mcx_quotes(mcx)
        q.update(qm)
        under.update(um)
    nse = [c for c in contracts_ if c["seg"] in ("NFO", "BFO")]
    stocks = {c["symbol"] for c in nse if c["symbol"] not in UNDER} | {w for w in want if w not in UNDER}
    if nse or stocks or any(w in UNDER and UNDER[w][2] for w in want):
        qa, ua = _angel_quotes(nse, tuple(sorted(stocks)))
        q.update(qa)
        under.update(ua)
    from .sync import now_ist
    status["quotes_at"] = now_ist().isoformat(timespec="seconds")
    return q, under


def _mcx_quotes(contracts_: list) -> tuple[dict, dict]:
    """MCX option quotes + the price of the futures each symbol's options settle into (nearest one ≥ the option's
    expiry)."""
    from . import chain, sources
    if not sources.Angel.available():
        return {}, {}
    toks, und = set(), {}
    for c in contracts_:
        toks.add(str(c["token"]))
        if c["symbol"] not in und:
            seg, name, _ = chain.contracts(c["symbol"])
            ex, tok, _ = chain.underlying(c["symbol"], seg, name, dt.date.fromisoformat(c["expiry"]))
            if tok:
                und[c["symbol"]] = str(tok)
                toks.add(str(tok))
    raw = sources.Angel.quotes({"MCX": sorted(toks)}, "FULL")
    q = {}
    for tok, r in raw.items():
        d = r.get("depth") or {}
        q[str(tok)] = {"ltp": r.get("ltp"), "bid": (d.get("buy") or [{}])[0].get("price") or None,
                       "ask": (d.get("sell") or [{}])[0].get("price") or None, "close": r.get("close")}
    return q, {s: q[t]["ltp"] for s, t in und.items() if t in q and q[t].get("ltp")}


def _angel_quotes(contracts_: list, stocks: tuple = ()) -> tuple[dict, dict]:
    from . import sources
    if not sources.Angel.available():
        return {}, {}
    ut = _under_tokens()
    for s_ in stocks:                                          # stock options: the stock's NSE cash price
        tok = (sources.Angel._tokens or {}).get(("NSE", s_.upper())) or (sources.Angel.token(s_) if s_ in OTHER_INDEX else None)
        if tok:
            ut[s_] = ("NSE", str(tok))
    by: dict = {}
    for c in contracts_:
        by.setdefault(c["seg"], set()).add(str(c["token"]))
    for ex, tok in ut.values():
        by.setdefault(ex, set()).add(tok)
    raw = sources.Angel.quotes({k: sorted(v) for k, v in by.items()}, "FULL")
    q = {}
    for tok, r in raw.items():
        d = r.get("depth") or {}
        bid = (d.get("buy") or [{}])[0].get("price") or None
        ask = (d.get("sell") or [{}])[0].get("price") or None
        q[str(tok)] = {"ltp": r.get("ltp"), "bid": bid, "ask": ask, "close": r.get("close")}
    under = {sym: q[tok]["ltp"] for sym, (ex, tok) in ut.items() if tok in q and q[tok].get("ltp")}
    return q, under


def market_open(now: dt.datetime, seg: str = "NFO") -> bool:
    if seg == "DELTA":
        return True                                           # crypto trades 24/7
    m = now.hour * 60 + now.minute
    from .sync import is_holiday
    if seg == "MCX":
        return now.weekday() < 5 and 9 * 60 <= m < 23 * 60 + 30 and not is_holiday("MCX", now)
    return now.weekday() < 5 and 9 * 60 + 15 <= m < 15 * 60 + 30 and not is_holiday("BSE" if seg == "BFO" else "NSE", now)


def day_key(t: dt.datetime, seg: str) -> dt.date:
    """Trading day: the IST date for NSE / BSE; the UTC date (05:30 → 05:30 IST) for crypto."""
    return (t - dt.timedelta(hours=5, minutes=30)).date() if seg == "DELTA" else t.date()


MCX_SQ = 23 * 60


def _sq_min(st: dict) -> int:
    h, m = st["settings"]["square_off"][:5].split(":")
    return int(h) * 60 + int(m)


def _mult(c: dict) -> float:
    return float(c.get("mult") or 1.0)


def money(st: dict, v: float) -> str:
    return f"${v:,.2f}" if st["settings"].get("currency") == "USD" else f"₹{v:,.0f}"


def open_block(st: dict, c: dict, now: dt.datetime) -> str | None:
    """Why a NEW position can't be opened now (None = it can)."""
    if c["seg"] == "DELTA":
        stl = settle_time(c)
        if stl and now >= stl - dt.timedelta(minutes=10):
            return "this contract settles in under 10 minutes — pick a later expiry"
        m = now.hour * 60 + now.minute
        if 5 * 60 + 15 <= m < 5 * 60 + 30:
            return "05:15–05:30 IST: the crypto day is closing (intraday square-off)"
        return None
    if c["seg"] == "MCX":
        if not market_open(now, "MCX"):
            return "MCX closed (09:00–23:30)"
        if now.hour * 60 + now.minute >= MCX_SQ:
            return "after the 23:00 MCX square-off — no new positions"
        if c.get("expiry") == now.date().isoformat():
            return "MCX option expiry day — pick the next expiry (expiring options turn into futures)"
        return None
    if not market_open(now):
        return "market closed (09:15–15:30)"
    if now.hour * 60 + now.minute >= _sq_min(st):
        return f"after the {st['settings']['square_off']} square-off time — no new positions"
    return None


def must_close(st: dict, p: dict, now: dt.datetime) -> str | None:
    """Why an open position has to be sold now (None = keep it)."""
    seg = p["seg"]
    if day_key(dt.datetime.fromisoformat(p["opened"]), seg) < day_key(now, seg):
        return "end of the crypto day (05:30 IST)" if seg == "DELTA" else "missed square-off — sold at the first live price"
    if seg == "DELTA":
        stl = settle_time(p)
        return "5 min before expiry" if stl and now >= stl - dt.timedelta(minutes=5) else None
    if seg == "MCX":
        m = now.hour * 60 + now.minute
        if p.get("expiry") == now.date().isoformat() and m >= 17 * 60:
            return "MCX option expiry day 17:00 (never let it turn into futures)"
        return "square-off 23:00 (MCX)" if m >= MCX_SQ else None
    if now.hour * 60 + now.minute >= _sq_min(st):
        return f"square-off {st['settings']['square_off']}"
    return None


# ───────────────────────────── the engine (pure: state + quotes → new state) ─────────────────────────────
def resolve_plan(plan: dict, c: dict, under: float | None, px: float, st: dict, now: dt.datetime) -> dict:
    """Relative stop / target → fixed levels at the fill. "index_pts": points from the index (a CE's stop below it, a
    PE's above); "option_pts": points from the option's own fill price (stop below, target above — bought options).
    A relative level that can't be fixed (no index price at a limit fill, stop at or below 0) is dropped, with an event."""
    plan = dict(plan)
    for k in ("stop", "target", "target2"):
        lv = plan.get(k)
        if not lv or lv.get("basis") not in ("index_pts", "option_pts"):
            continue
        pts = float(lv["price"])
        if lv["basis"] == "index_pts":
            sgn = 1 if c["type"] == "CE" else -1
            lvl = None if under is None else round(under + pts * (-sgn if k == "stop" else sgn), 2)
            basis = "index"
        else:
            lvl = round(px - pts if k == "stop" else px + pts, 2)
            basis = "option"
        if lvl is None or lvl <= 0:
            plan[k] = None
            _event(st, "STOP", f"{c['tsym']}: {k} of {pts:g} {lv['basis'].replace('_', ' ')} could not be set — set it on the position", now)
        else:
            plan[k] = {**lv, "basis": basis, "price": lvl}
    return plan


def _exec(st: dict, pos: dict | None, c: dict, side: str, lots: int, px: float, now: dt.datetime, why: str,
          source: str, under: float | None, plan: dict | None = None) -> dict:
    """Execute a fill. Positions carry a SIGNED qty: > 0 long (bought options), < 0 short (sold options — strategy
    legs only). A fill in the position's direction opens / adds; the other way reduces / closes. Returns the position."""
    mu = _mult(c)
    sgn = 1 if side == "BUY" else -1
    if pos is None:
        pos = {"id": uuid.uuid4().hex[:10], **{k: c[k] for k in ("symbol", "seg", "token", "tsym", "lot", "expiry",
                                                                 "strike", "type")}, "mult": mu,
               "lots": 0, "qty": 0, "avg": 0.0, "opened": now.isoformat(timespec="seconds"), "source": source,
               "under_entry": under, "charges": 0.0, "realized": 0.0, "stop": None, "target": None,
               "be_after_target": False, "fills": [], "note": "", "dir": sgn}
        if plan:
            plan = resolve_plan(plan, c, under, px, st, now)
            pos.update({k: v for k, v in plan.items() if k in ("stop", "target", "target2", "be_after_target", "note", "grp")})
        pos["plan0"] = {"stop": pos.get("stop"), "target": pos.get("target")}     # as set at entry (for review)
        st["positions"].append(pos)
    cur = pos["qty"]
    if cur == 0 or (cur > 0) == (sgn > 0):                     # open / add
        qty = lots * c["lot"]
        ch = charges(side, px, qty, c["seg"], st["settings"]["charges"], under, mu)
        pos["avg"] = (pos["avg"] * abs(cur) + px * qty) / (abs(cur) + qty)
        pos["qty"] = cur + sgn * qty
        st["cash"] += -sgn * px * qty * mu - ch["total"]       # buying pays the premium, selling receives it
    else:                                                       # reduce / close
        lots = min(lots, abs(cur) // c["lot"])
        qty = lots * c["lot"]
        ch = charges(side, px, qty, c["seg"], st["settings"]["charges"], under, mu)
        pos["realized"] += (px - pos["avg"]) * qty * mu * (1 if cur > 0 else -1)
        pos["qty"] = cur + sgn * qty
        st["cash"] += -sgn * px * qty * mu - ch["total"]
    pos["lots"] = pos["qty"] // c["lot"] if pos["qty"] >= 0 else -((-pos["qty"]) // c["lot"])
    pos["charges"] += ch["total"]
    pos["fills"].append({"at": now.isoformat(timespec="seconds"), "side": side, "lots": lots, "price": round(px, 2),
                         "why": why, "under": under, "charges": ch["total"]})
    unit = "contract" if c["seg"] == "DELTA" else "lot"
    _event(st, "FILL", f"{side} {lots} {unit}{'s' if lots > 1 else ''} {c['tsym']} @ {px:.2f} ({why}) · charges {money(st, ch['total'])}", now)
    if pos["qty"] == 0:
        st["positions"].remove(pos)
        net = pos["realized"] - pos["charges"]
        open_side = "BUY" if pos.get("dir", 1) > 0 else "SELL"
        opens = [f for f in pos["fills"] if f["side"] == open_side]
        st["trades"].append({**{k: pos[k] for k in ("id", "symbol", "tsym", "expiry", "strike", "type", "source",
                                                   "opened", "under_entry", "fills", "note")},
                             "plan0": pos.get("plan0"), "stop_last": pos.get("stop"), "grp": pos.get("grp"),
                             "side": "LONG" if open_side == "BUY" else "SHORT",
                             "closed": now.isoformat(timespec="seconds"), "why": why, "gross": round(pos["realized"], 4),
                             "charges": round(pos["charges"], 4), "net": round(net, 4),
                             "lots": max(f["lots"] for f in opens), "lot": pos["lot"], "mult": _mult(pos), "seg": pos["seg"],
                             "qty": sum(f["lots"] for f in opens) * pos["lot"],
                             "currency": st["settings"].get("currency", "INR"),
                             "entry": round(opens[0]["price"], 2), "exit": round(px, 2), "under_exit": under})
        _event(st, "CLOSED", f"{c['tsym']} closed ({why}): net {'+' if net >= 0 else '−'}{money(st, abs(net))} after "
                             f"{money(st, pos['charges'])} charges", now)
    return pos


def close_side(p: dict) -> str:
    return "SELL" if p["qty"] > 0 else "BUY"


def close_price(p: dict, qc: dict | None) -> float | None:
    """What closing costs now: a long sells at the bid, a short buys back at the ask."""
    return fill_price(close_side(p), qc)


def mark(p: dict, qc: dict | None = None) -> float:
    """Unrealised P&L of an open position at its closing price."""
    qc = qc or {"bid": p.get("bid"), "ask": p.get("ask"), "ltp": p.get("ltp")}
    px = close_price(p, qc) or p["avg"]
    return (px - p["avg"]) * p["qty"] * _mult(p)


def place(st: dict, order: dict, q: dict, under: dict, now: dt.datetime) -> dict:
    """Validate and place an order (fills at once if marketable). order: symbol expiry strike type side lots
    order_type (MARKET | LIMIT) limit stop target be_after_target note source. Returns the order record."""
    c = order["contract"]
    lots = int(order.get("lots") or 0)
    rec = {"id": uuid.uuid4().hex[:10], "at": now.isoformat(timespec="seconds"), "tsym": c["tsym"], "side": order["side"],
           "lots": lots, "order_type": order.get("order_type", "MARKET"), "limit": order.get("limit"),
           "status": "rejected", "source": order.get("source", "manual"), "contract": c,
           "plan": {k: order.get(k) for k in ("stop", "target", "target2", "be_after_target", "note")}}
    pos = next((p for p in st["positions"] if p["token"] == c["token"]), None)

    def reject(why):
        rec["reason"] = why
        st["orders"].append(rec)
        _event(st, "REJECTED", f"{order['side']} {c['tsym']}: {why}", now)
        return rec

    blk = open_block(st, c, now)
    opening = (order["side"] == "BUY" and not (pos and pos["qty"] < 0)) or bool(order.get("open_short"))
    if blk and (opening or not market_open(now, c["seg"])):
        return reject(blk)
    if lots < 1:
        return reject("lots must be ≥ 1")
    if opening and rec["source"] == "manual" and len((order.get("note") or "").strip()) < 3:
        return reject("write why you are taking this trade (the 'Why this trade?' box) — it's required")
    if order["side"] == "SELL" and not order.get("open_short") and (pos is None or pos["lots"] < lots):
        return reject("option buying only — you can sell only what you hold (selling options: use the strategy builder)")
    if (pos["lots"] if pos and pos["qty"] * (1 if order["side"] == "BUY" else -1) > 0 else 0) + lots > st["settings"]["max_lots"]:
        return reject(f"more than {st['settings']['max_lots']} lots (risk limit in the settings)")
    qc = q.get(c["token"])
    if not qc or not qc.get("ltp"):
        return reject("no live quote for this contract")
    px = fill_price(order["side"], qc)
    if rec["order_type"] == "LIMIT":
        lim = float(order["limit"])
        marketable = px <= lim if order["side"] == "BUY" else px >= lim
        if not marketable:
            rec["status"] = "open"
            st["orders"].append(rec)
            _event(st, "ORDER", f"{order['side']} {lots} lot {c['tsym']} LIMIT {lim:.2f} placed", now)
            return rec
    plan = rec["plan"]
    u = under.get(c["symbol"])
    if u is None and any((plan.get(k) or {}).get("basis") == "index_pts" for k in ("stop", "target")):
        return reject("no live index price for an index-points stop / target")
    if order["side"] == "BUY" and not (pos and pos["qty"] < 0):          # buying (not covering a short) needs cash
        need = px * lots * c["lot"] * _mult(c) + charges("BUY", px, lots * c["lot"], c["seg"], st["settings"]["charges"],
                                                         u, _mult(c))["total"]
        if need > free_cash(st):
            return reject(f"not enough cash: needs {money(st, need)}, have {money(st, free_cash(st))}")
    p = _exec(st, pos, c, order["side"], lots, px, now, "market" if rec["order_type"] == "MARKET" else "limit",
              rec["source"], under.get(c["symbol"]), rec["plan"])
    rec.update(status="filled", fill_price=round(px, 2), fill_at=now.isoformat(timespec="seconds"), position=p["id"])
    st["orders"].append(rec)
    return rec


def step(st: dict, q: dict, under: dict, now: dt.datetime) -> int:
    """One monitoring pass: open limit orders, stops, targets, square-off. Returns the number of fills."""
    n = 0
    for o in [o for o in st["orders"] if o["status"] == "open"]:
        c = o["contract"]
        stale = day_key(dt.datetime.fromisoformat(o["at"]), c["seg"]) < day_key(now, c["seg"])
        if stale or (o["side"] == "BUY" and open_block(st, c, now)) or not market_open(now, c["seg"]):
            o["status"] = "cancelled"
            o["reason"] = "square-off / market closed"
            continue
        qc = q.get(c["token"])
        px = fill_price(o["side"], qc)
        if px is None:
            continue
        lim = float(o["limit"])
        if (o["side"] == "BUY" and px <= lim) or (o["side"] == "SELL" and px >= lim):
            pos = next((p for p in st["positions"] if p["token"] == c["token"]), None)
            if o["side"] == "SELL" and pos is None:
                o["status"] = "cancelled"
                continue
            need = px * o["lots"] * c["lot"] * _mult(c)
            if o["side"] == "BUY" and need > free_cash(st):
                o["status"], o["reason"] = "rejected", "not enough cash at fill time"
                continue
            p = _exec(st, pos, c, o["side"], o["lots"], px, now, "limit", o["source"], under.get(c["symbol"]), o["plan"])
            o.update(status="filled", fill_price=round(px, 2), fill_at=now.isoformat(timespec="seconds"), position=p["id"])
            n += 1
    for p in list(st["positions"]):
        qc = q.get(p["token"])
        u = under.get(p["symbol"])
        if qc:
            p["ltp"], p["bid"], p["ask"] = qc.get("ltp"), qc.get("bid"), qc.get("ask")
        if u is not None:
            p["under_ltp"] = u
        p["marked_at"] = now.isoformat(timespec="seconds")
        if not market_open(now, p["seg"]):
            continue
        px = close_price(p, qc)
        if px is None:
            continue
        why = must_close(st, p, now)                          # square-off / day end / expiry: never carry forward
        if why:
            _exec(st, p, p, close_side(p), abs(p["lots"]), px, now, why, p["source"], u)
            n += 1
            continue
        if p.get("grp"):                                      # strategy legs: stop / target on the whole strategy
            continue
        if level_hit(p.get("stop"), "stop", p["type"], qc, u):
            _exec(st, p, p, "SELL", p["lots"], px, now, "stop", p["source"], u)
            n += 1
            continue
        t = p.get("target")
        if t and level_hit(t, "target", p["type"], qc, u):
            lots = min(int(t.get("lots") or p["lots"]), p["lots"])
            p["target"] = None
            _exec(st, p, p, "SELL", lots, px, now, "target", p["source"], u)
            n += 1
            if p["qty"] > 0 and p.get("be_after_target"):
                p["stop"] = ({"basis": "index", "price": p["under_entry"]} if p.get("stop", {}) and p["stop"].get("basis") == "index"
                             and p.get("under_entry") else {"basis": "option", "price": round(p["avg"], 2)})
                _event(st, "STOP", f"{p['tsym']}: stop moved to cost ({p['stop']['basis']} {p['stop']['price']})", now)
                if p.get("target2"):                          # optional 2nd target for the rest
                    p["target"] = p.pop("target2")
                    _event(st, "TARGET", f"{p['tsym']}: 2nd target set ({p['target']['basis']} {p['target']['price']})", now)
    n += _groups_step(st, q, under, now)
    return n


# ───────────────────────────── strategies (multi-leg, may include sold options) ─────────────────────────────
def margin_used(st: dict) -> float:
    return sum(g["margin"] for g in st.get("groups", []) if g.get("open"))


def free_cash(st: dict) -> float:
    """Cash not blocked as margin for sold strategy legs."""
    return st["cash"] - margin_used(st)


def group_pnl(st: dict, g: dict) -> float | None:
    """Open strategy P&L in ₹: realised on legs closed so far + unrealised at closing prices − charges.
    None if any open leg has no quote."""
    tot = 0.0
    for p in st["positions"]:
        if p.get("grp") != g["id"]:
            continue
        if close_price(p, {"bid": p.get("bid"), "ask": p.get("ask"), "ltp": p.get("ltp")}) is None:
            return None
        tot += mark(p) + p["realized"] - p["charges"]
    tot += sum(t["net"] for t in st["trades"] if t.get("grp") == g["id"])
    return tot


def exit_group(st: dict, g: dict, q: dict, under: dict, now: dt.datetime, why: str) -> int:
    """Close every leg of a strategy: shorts first (buy back), then longs, so margin never goes uncovered."""
    n = 0
    legs = sorted([p for p in st["positions"] if p.get("grp") == g["id"]], key=lambda p: p["qty"] > 0)
    for p in legs:
        qc = q.get(p["token"]) or {"bid": p.get("bid"), "ask": p.get("ask"), "ltp": p.get("ltp")}
        px = close_price(p, qc)
        if px is None:
            continue
        _exec(st, p, p, close_side(p), abs(p["lots"]), px, now, why, p["source"], under.get(p["symbol"]))
        n += 1
    _close_group_if_done(st, g, now)
    return n


def _close_group_if_done(st: dict, g: dict, now: dt.datetime) -> None:
    if g.get("open") and not any(p.get("grp") == g["id"] for p in st["positions"]):
        g["open"] = False
        g["closed"] = now.isoformat(timespec="seconds")
        g["net"] = round(sum(t["net"] for t in st["trades"] if t.get("grp") == g["id"]), 2)
        _event(st, "CLOSED", f"strategy {g['name']} closed: net {'+' if g['net'] >= 0 else '−'}{money(st, abs(g['net']))} "
                             f"· margin {money(st, g['margin'])} released", now)


def _groups_step(st: dict, q: dict, under: dict, now: dt.datetime) -> int:
    n = 0
    for g in [g for g in st.get("groups", []) if g.get("open")]:
        _close_group_if_done(st, g, now)                      # legs squared off individually above
        if not g.get("open"):
            continue
        pnl = group_pnl(st, g)
        if pnl is None:
            continue
        g["pnl"] = round(pnl, 2)
        if g.get("stop_rs") and pnl <= -abs(float(g["stop_rs"])):
            n += exit_group(st, g, q, under, now, "strategy stop")
        elif g.get("target_rs") and pnl >= abs(float(g["target_rs"])):
            n += exit_group(st, g, q, under, now, "strategy target")
    return n


def place_strategy(st: dict, legs: list[dict], q: dict, under: dict, now: dt.datetime, name: str, margin: float,
                   stop_rs: float | None, target_rs: float | None, note: str, source: str = "manual") -> dict:
    """Enter all legs of a strategy at market (longs first, so a short is never naked while the others fill).
    legs: [{contract, side BUY|SELL, lots}]. margin: ₹ blocked while the strategy is open (its max loss when that is
    limited, else an exchange-style estimate). All-or-nothing: any problem → nothing is placed."""
    if len((note or "").strip()) < 3:
        return {"status": "rejected", "reason": "write why you are taking this trade — it's required"}
    if not legs:
        return {"status": "rejected", "reason": "no legs"}
    if len({l["contract"]["seg"] for l in legs}) > 1 or len({l["contract"]["symbol"] for l in legs}) > 1:
        return {"status": "rejected", "reason": "all legs must be on the same underlying"}
    seg = legs[0]["contract"]["seg"]
    if seg == "DELTA":
        return {"status": "rejected", "reason": "strategies are for NSE / BSE / MCX options"}
    if not market_open(now, seg):
        return {"status": "rejected", "reason": "market closed"}
    for l in legs:
        blk = open_block(st, l["contract"], now)
        if blk:
            return {"status": "rejected", "reason": blk}
        if any(p["token"] == l["contract"]["token"] for p in st["positions"]):
            return {"status": "rejected", "reason": f"you already hold {l['contract']['tsym']} — close it first"}
        if int(l.get("lots") or 0) < 1 or int(l["lots"]) > st["settings"]["max_lots"]:
            return {"status": "rejected", "reason": f"lots per leg must be 1–{st['settings']['max_lots']}"}
    if len({l["contract"]["token"] for l in legs}) < len(legs):
        return {"status": "rejected", "reason": "the same contract appears twice"}
    px = {}
    for l in legs:
        qc = q.get(l["contract"]["token"])
        p = fill_price(l["side"], qc) if qc and qc.get("ltp") else None
        if p is None:
            return {"status": "rejected", "reason": f"no live quote for {l['contract']['tsym']}"}
        px[l["contract"]["token"]] = p
    debit = sum((1 if l["side"] == "BUY" else -1) * px[l["contract"]["token"]] * int(l["lots"]) * l["contract"]["lot"]
                for l in legs)
    need = max(debit, 0) + max(float(margin or 0), 0) + 100 * len(legs)          # ~charges headroom
    if need > free_cash(st):
        return {"status": "rejected", "reason": f"not enough cash: needs ≈{money(st, need)} (premium + margin), "
                                                f"have {money(st, free_cash(st))}"}
    gid = uuid.uuid4().hex[:8]
    g = {"id": gid, "name": name, "symbol": legs[0]["contract"]["symbol"], "opened": now.isoformat(timespec="seconds"),
         "margin": round(float(margin or 0), 2), "stop_rs": stop_rs, "target_rs": target_rs, "note": note.strip(),
         "open": True, "legs": [{"tsym": l["contract"]["tsym"], "side": l["side"], "lots": int(l["lots"])} for l in legs]}
    st.setdefault("groups", []).append(g)
    _event(st, "STRATEGY", f"{name}: {len(legs)} legs · margin blocked {money(st, g['margin'])}", now)
    u = under.get(g["symbol"])
    for l in sorted(legs, key=lambda l: l["side"] != "BUY"):
        c = l["contract"]
        _exec(st, None, c, l["side"], int(l["lots"]), px[c["token"]], now, "market", source, u,
              {"note": note.strip(), "grp": gid})
    return {"status": "filled", "group": g}


# ───────────────────────────── service ─────────────────────────────
def snapshot(account: str) -> dict:
    from .sync import now_ist
    with _lock:
        st = load(account)
    now = now_ist()
    unreal = sum(mark(p) for p in st["positions"])
    seg = "DELTA" if account == "crypto" else "MCX" if account == "ng" else "NFO"
    today = day_key(now, seg)
    td = [t for t in st["trades"] if day_key(dt.datetime.fromisoformat(t["closed"]), seg) == today]
    allt = st["trades"]
    wins = [t for t in allt if t["net"] > 0]
    invested = sum(p["avg"] * p["qty"] * _mult(p) for p in st["positions"])
    return {"account": account, "settings": st["settings"], "cash": round(st["cash"], 2),
            "equity": round(st["cash"] + invested + unreal, 2), "unrealized": round(unreal, 2),
            "margin": round(margin_used(st), 2), "groups": [g for g in st.get("groups", []) if g.get("open")]
                                                           + [g for g in st.get("groups", []) if not g.get("open")][-10:],
            "today": {"trades": len(td), "net": round(sum(t["net"] for t in td), 2)},
            "all": {"trades": len(allt), "net": round(sum(t["net"] for t in allt), 2),
                    "win": round(len(wins) / len(allt), 3) if allt else None,
                    "charges": round(sum(t["charges"] for t in allt), 2)},
            "positions": st["positions"], "orders": [o for o in st["orders"] if o["status"] == "open"],
            "recent_orders": st["orders"][-15:], "trades": allt[-60:], "events": st["events"][-60:],
            "market_open": market_open(now, seg) or (account == "manual" and market_open(now, "MCX")), "currency": st["settings"].get("currency", "INR"),
            "status": status, "now": now.isoformat(timespec="seconds")}


def contract_list(symbol: str, expiry: str | None = None) -> dict:
    if symbol not in UNDER and not is_stock(symbol):
        raise KeyError(symbol)
    seg, name, rows = contracts(symbol)
    exps = sorted({r[0] for r in rows})
    if not exps:
        return {"symbol": symbol, "error": "no live contracts (Angel scrip master not loaded?)"}
    e = dt.date.fromisoformat(expiry) if expiry and dt.date.fromisoformat(expiry) in exps else exps[0]
    ks = sorted({r[1] for r in rows if r[0] == e})
    lot = next((r[5] for r in rows if r[0] == e), None)
    from .sync import now_ist
    if seg == "DELTA":                                            # skip contracts settling within 10 minutes
        now = now_ist()
        exps = [x for x in exps if now < dt.datetime.combine(x, dt.time(17, 30)) - dt.timedelta(minutes=10)]
        if not exps:
            return {"symbol": symbol, "error": "no BTC expiry open for trading"}
        e = dt.date.fromisoformat(expiry) if expiry and dt.date.fromisoformat(expiry) in exps else exps[0]
        ks = sorted({r[1] for r in rows if r[0] == e})
    if seg == "MCX":                                              # the futures the options are on
        probe = next(r for r in rows if r[0] == e)
        q, under = quotes([{"symbol": symbol, "seg": "MCX", "token": str(probe[3]), "expiry": e.isoformat()}])
    else:
        q, under = quotes([], (symbol,))
    spot = under.get(symbol)
    atm = min(ks, key=lambda k: abs(k - spot)) if spot and ks else None
    return {"symbol": symbol, "expiries": [x.isoformat() for x in exps], "expiry": e.isoformat(), "strikes": ks,
            "lot": lot, "spot": spot, "atm": atm,
            "step": STEP.get(symbol) or (min(b - a for a, b in zip(ks, ks[1:])) if len(ks) > 1 else 1),
            "stock": symbol not in UNDER, "mult": BTC_MULT if seg == "DELTA" else 1,
            "account": "crypto" if seg == "DELTA" else "manual",
            "settles": "17:30 IST" if seg == "DELTA" else "23:30 (no buys on expiry day)" if seg == "MCX" else "15:30"}


def quote(symbol: str, expiry: str, strike: float, typ: str, account: str = "manual") -> dict:
    c = find(symbol, expiry, strike, typ)
    if c is None:
        raise KeyError("no such contract")
    q, under = quotes([c], (symbol,))
    qc = q.get(c["token"]) or {}
    st = load("crypto" if c["seg"] == "DELTA" else account)
    px = fill_price("BUY", qc)
    est = charges("BUY", px, c["lot"], c["seg"], st["settings"]["charges"], under.get(symbol), _mult(c)) if px else None
    return {"contract": c, "quote": qc, "under": under.get(symbol),
            "buy_1_lot": round(px * c["lot"] * _mult(c), 4) if px else None, "charges_buy_1_lot": est,
            "currency": st["settings"].get("currency", "INR")}


def order(account: str, body: dict) -> dict:
    from .sync import now_ist
    c = find(body["symbol"], body["expiry"], float(body["strike"]), body["type"])
    if c is None:
        raise KeyError("no such contract")
    if (c["seg"] == "DELTA") != (account == "crypto"):
        raise KeyError("BTC options trade in the crypto account, NIFTY / SENSEX in the ₹ accounts")
    with _lock:
        st = load(account)
        q, under = quotes([c] + st["positions"], (c["symbol"],))
        rec = place(st, {**body, "contract": c, "source": body.get("source", "bot") if account == "bot" else "manual"},
                    q, under, now_ist())
        save(st)
    return rec


def exit_position(account: str, pid: str, lots: int | None = None) -> dict:
    from .sync import now_ist
    with _lock:
        st = load(account)
        p = next((p for p in st["positions"] if p["id"] == pid), None)
        if p is None:
            raise KeyError(pid)
        if p.get("grp"):
            return {"status": "rejected", "reason": "this is a strategy leg — use “Exit strategy” (closing one leg alone "
                                                    "could leave a sold option uncovered)"}
        q, under = quotes([p], (p["symbol"],))
        rec = place(st, {"contract": {k: p.get(k) for k in ("symbol", "seg", "token", "tsym", "lot", "expiry", "strike", "type",
                                                            "mult")},
                         "side": close_side(p), "lots": lots or abs(p["lots"]), "order_type": "MARKET", "source": p["source"]},
                    q, under, now_ist())
        save(st)
    return rec


def exit_strategy(account: str, gid: str) -> dict:
    from .sync import now_ist
    with _lock:
        st = load(account)
        g = next((g for g in st.get("groups", []) if g["id"] == gid and g.get("open")), None)
        if g is None:
            raise KeyError(gid)
        legs = [p for p in st["positions"] if p.get("grp") == gid]
        q, under = quotes(legs, (g["symbol"],))
        now = now_ist()
        if not market_open(now, legs[0]["seg"] if legs else "NFO"):
            return {"status": "rejected", "reason": "market closed"}
        n = exit_group(st, g, q, under, now, "manual exit")
        save(st)
    return {"status": "filled" if n else "rejected", "group": g, **({} if n else {"reason": "no live quotes"})}


def strategy_order(account: str, body: dict) -> dict:
    """body: {name, legs: [{symbol, expiry, strike, type, side, lots}], margin, stop_rs, target_rs, note}"""
    from .sync import now_ist
    legs = []
    for l in body.get("legs") or []:
        c = find(l["symbol"], l["expiry"], float(l["strike"]), l["type"])
        if c is None:
            return {"status": "rejected", "reason": f"no such contract {l['symbol']} {l['expiry']} {l['strike']} {l['type']}"}
        if l.get("side") not in ("BUY", "SELL"):
            return {"status": "rejected", "reason": "side must be BUY or SELL"}
        legs.append({"contract": c, "side": l["side"], "lots": int(l.get("lots") or 1)})
    num = lambda v: float(v) if v not in (None, "", 0, "0") else None
    with _lock:
        st = load(account)
        q, under = quotes([l["contract"] for l in legs] + st["positions"], tuple({l["contract"]["symbol"] for l in legs}))
        r = place_strategy(st, legs, q, under, now_ist(), str(body.get("name") or "Strategy")[:40], float(body.get("margin") or 0),
                           num(body.get("stop_rs")), num(body.get("target_rs")), str(body.get("note") or ""))
        if r["status"] == "filled":
            save(st)
    return r


def modify(account: str, pid: str, body: dict) -> dict:
    with _lock:
        st = load(account)
        p = next((p for p in st["positions"] if p["id"] == pid), None)
        if p is None:
            raise KeyError(pid)
        if p.get("grp"):
            raise ValueError("strategy legs: set the stop / target on the whole strategy")
        for k in ("stop", "target"):
            v = body.get(k)
            if v and v.get("price") not in (None, "") and (v.get("basis") not in ("index", "option") or float(v["price"]) <= 0):
                raise ValueError(f"{k}: basis must be 'index' or 'option' and the price above 0")
        for k in ("stop", "target"):
            if k in body:
                v = body[k]
                p[k] = None if not v or v.get("price") in (None, "") else {"basis": v["basis"], "price": float(v["price"]),
                                                                            **({"lots": int(v["lots"])} if v.get("lots") else {})}
        if "be_after_target" in body:
            p["be_after_target"] = bool(body["be_after_target"])
        save(st)
        return p


def cancel(account: str, oid: str) -> dict:
    with _lock:
        st = load(account)
        o = next((o for o in st["orders"] if o["id"] == oid and o["status"] == "open"), None)
        if o is None:
            raise KeyError(oid)
        o["status"] = "cancelled"
        save(st)
        return o


def set_settings(account: str, body: dict) -> dict:
    with _lock:
        st = load(account)
        s = st["settings"]
        for k in ("square_off", "max_lots", "auto_morning_edge", "bot_lots", "usd_inr"):
            if k in body:
                s[k] = body[k]
        if "me_target2" in body:
            s["me_target2"] = max(0.0, float(body["me_target2"] or 0))
        if "charges" in body:
            s["charges"].update({k: float(v) for k, v in body["charges"].items() if k in s["charges"]})
        save(st)
        return s


def reset(account: str, capital: float | None = None) -> dict:
    with _lock:
        from . import db
        old = load(account)
        st = _new(account, old["settings"])
        if capital:
            st["settings"]["capital"] = float(capital)
            st["cash"] = float(capital)
        if old["trades"] or old["positions"] or old["events"]:
            db.paper_archive(account)                            # history kept under e.g. manual@20261017-153000
        save(st)
        return {"reset": account, "cash": st["cash"]}


# ───────────────────────────── journal view of paper trades ─────────────────────────────
LABEL = {"manual": "Paper · mine", "crypto": "Paper · crypto", "bot": "Paper · ME bot", "gann": "Paper · Gann bot",
         "plan": "Paper · Daily Plan bot", "ng": "Paper · NG bot"}
LOT = {"NIFTY 50": 65, "SENSEX": 20, "BTC": 1}


def journal_rows() -> list:
    """Closed paper trades of the current accounts in the practice-journal format (📊 Stats). Crypto P&L in ₹ at the
    account's USD→INR rate so all accounts add up."""
    from . import db, store
    import pandas as pd
    with db.conn() as c:
        rows = [(r["account"], json.loads(r["data"])) for r in
                c.execute("SELECT account, data FROM paper_trade WHERE account NOT LIKE '%@%' ORDER BY closed")]
        fx = {r["account"]: json.loads(r["settings"]).get("usd_inr", 1.0) for r in c.execute(
            "SELECT account, settings FROM paper_account")}
    out = []
    for acct, t in rows:
        mult = t.get("mult") or (BTC_MULT if t.get("symbol") == "BTC" else 1.0)
        qty = t.get("qty") or (t.get("lots") or 1) * LOT.get(t.get("symbol"), 1)
        rate = fx.get(acct, 1.0) if acct == "crypto" else 1.0
        ep = lambda s: int(store.to_epoch(pd.DatetimeIndex([pd.Timestamp(s)]))[0])
        dur = (pd.Timestamp(t["closed"]) - pd.Timestamp(t["opened"])).total_seconds() / 60
        sd = "SELL" if t.get("side") == "SHORT" else "BUY"
        out.append({"id": f"paper-{acct}-{t['id']}", "symbol": "BTCUSD" if t.get("symbol") == "BTC" else t.get("symbol"),
                    "tf": "1" if dur <= 90 else "5", "side": f"{sd} {t.get('type')}",
                    "instrument": f"{sd} {t.get('type')}", "qty": qty, "entry": t.get("entry"), "exit": t.get("exit"),
                    "entry_time": ep(t["opened"]), "exit_time": ep(t["closed"]),
                    "pnl_pts": round((t.get("exit", 0) - t.get("entry", 0)) * (-1 if sd == "SELL" else 1), 2), "pnl_rs": round(t.get("net", 0) * rate, 2),
                    "reason": t.get("why"), "mode": "paper", "tag": LABEL.get(acct, acct),
                    "note": " · ".join(x for x in (t.get("tsym"), t.get("note"),
                                                    f"net ${t['net']:.2f}" if acct == "crypto" else "") if x)})
    return out


# ───────────────────────────── Morning Edge bot ─────────────────────────────
def itm200(spot: float, signal: int, step: int = 50) -> tuple[float, str]:
    k = round(spot / step) * step
    return (k - 200, "CE") if signal > 0 else (k + 200, "PE")


def target2(st: dict) -> dict:
    """{"target2": …} when the account's optional Morning Edge 2nd target is on, else {}"""
    pts = float(st["settings"].get("me_target2") or 0)
    return {"target2": {"basis": "index_pts", "price": pts}} if pts > 0 else {}


def bot_morning_edge(decision: dict) -> dict | None:
    """Called after the live 09:45 decision. Trades the signal in the bot account (once per day)."""
    from .sync import now_ist
    from .edge import THRESH
    sig = int(decision.get("signal") or 0)
    with _lock:
        st = load("bot")
        if not st["settings"].get("auto_morning_edge", True):
            return None
        day = decision.get("day")
        if not sig:                                          # log the look, so an empty bot account shows it is alive
            if not any(e["text"].startswith(f"Morning Edge {day}") for e in st["events"]):
                _event(st, "NO TRADE", f"Morning Edge {day}: pred {float(decision.get('pred') or 0):+.3f} is inside "
                                       f"±{THRESH} — no trade today", now_ist())
                save(st)
            return None
        if any(t.get("note", "").startswith(f"Morning Edge {day}") for t in st["trades"]) or \
           any(p.get("note", "").startswith(f"Morning Edge {day}") for p in st["positions"]):
            return None
        _, under = quotes([], ("NIFTY 50",))
        spot = under.get("NIFTY 50")
        if not spot:
            _event(st, "REJECTED", "Morning Edge: no live NIFTY price", now_ist())
            save(st)
            return None
        exps = sorted({r[0] for r in contracts("NIFTY 50")[2] if r[0] >= now_ist().date()})
        if not exps:
            return None
        K, typ = itm200(spot, sig)
        c = find("NIFTY 50", exps[0].isoformat(), K, typ)
        if c is None:
            _event(st, "REJECTED", f"Morning Edge: contract NIFTY {exps[0]} {K} {typ} not found", now_ist())
            save(st)
            return None
        q, under = quotes([c])
        lots = int(st["settings"].get("bot_lots", BOT_LOTS))
        u = under.get("NIFTY 50", spot)
        plan = {"stop": {"basis": "index", "price": u - 35 * sig},
                "target": {"basis": "index", "price": u + 35 * sig, "lots": max(1, lots // 2)},
                "be_after_target": True, **target2(st),
                "note": f"Morning Edge {day} pred {decision.get('pred', 0):+.3f} → {'BUY' if sig > 0 else 'SELL'} NIFTY"}
        rec = place(st, {"contract": c, "side": "BUY", "lots": lots, "order_type": "MARKET", "source": "bot:morning_edge",
                         **plan}, q, under, now_ist())
        save(st)
        log.info("paper bot morning edge %s: %s", day, rec.get("status"))
        return rec


# ───────────────────────────── background monitor ─────────────────────────────
def tick(now: dt.datetime | None = None, accounts: tuple = ACCOUNTS) -> int:
    from .sync import now_ist
    now = now or now_ist()
    n = 0
    with _lock:
        books = [load(a) for a in accounts]
        live = [st for st in books if st["positions"] or any(o["status"] == "open" for o in st["orders"])]
        if not live:
            return 0
        cs = [p for st in live for p in st["positions"]] + [o["contract"] for st in live for o in st["orders"]
                                                             if o["status"] == "open"]
        q, under = quotes(cs, tuple({c["symbol"] for c in cs}))
        if not q:
            return 0
        for st in live:
            n += step(st, q, under, now)
            save(st)
    return n


def loop():
    from .sync import now_ist
    while True:
        try:
            t = now_ist()
            m = t.hour * 60 + t.minute
            nse = t.weekday() < 5 and 9 * 60 + 14 <= m <= 15 * 60 + 31
            busy = lambda a: bool((x := load(a))["positions"] or any(o["status"] == "open" for o in x["orders"]))
            crypto = busy("crypto")                          # crypto positions are watched 24/7
            mcx_hours = t.weekday() < 5 and 9 * 60 <= m <= 23 * 60 + 31

            def has_mcx(a):
                x = load(a)
                return any(p["seg"] == "MCX" for p in x["positions"]) or \
                    any(o["status"] == "open" and o["contract"]["seg"] == "MCX" for o in x["orders"])
            mcx_accts = tuple(a for a in ("manual", "ng") if mcx_hours and has_mcx(a))
            if nse or crypto or mcx_accts:
                status["state"] = "watching"
                accts = tuple(a for a in ACCOUNTS if (nse and a not in ("crypto", "ng")) or (a == "crypto" and crypto)
                              or a in mcx_accts)
                tick(t, accts)
                status["error"] = None
                time.sleep(2 if nse else 3)
                continue
            status["state"] = "idle (market closed)"
        except Exception as e:
            log.exception("paper loop")
            status["error"] = str(e)[:200]
        time.sleep(5)                                         # a new BTC position is watched within 5 s


def start():
    if config.OFFLINE:
        return
    DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=loop, daemon=True, name="paper").start()
