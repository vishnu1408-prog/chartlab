"""Option strategy builder: payoff, risk numbers and a live quote for multi-leg option positions (paper only).

All legs share one underlying and one expiry. Prices: a bought leg at the ask, a sold leg at the bid (what a market
order would get now). "Today" values use Black's formula with each leg's own implied volatility (solved from its mid
price), so the today-curve starts at the current mark of the position; "at expiry" is plain intrinsic value.

Margin for sold options is an ESTIMATE: when the risk is limited (every sold option is covered by a bought one of the
same type) it is the worst-case cost of buying the position back (spread width for a credit spread, nothing for a
debit spread — its premium is paid up front); otherwise a SPAN-like share of the underlying's value per uncovered
short (exchange margins change daily — the broker's number is the real one).
"""
from __future__ import annotations

import datetime as dt
import math

from .options import _ncdf, bs

SPAN_PCT = {"NFO": 0.12, "BFO": 0.12, "STOCK": 0.20, "MCX CRUDEOIL": 0.12, "MCX NATURALGAS": 0.30,
            "MCX GOLD": 0.08, "MCX SILVER": 0.10}


def _q(l: dict) -> int:
    """Signed quantity in units: + bought, − sold."""
    return (1 if l["side"] == "BUY" else -1) * int(l["lots"]) * int(l["lot"])


def expiry_pnl(legs: list, S: float) -> float:
    return sum(_q(l) * ((max(S - l["strike"], 0) if l["type"] == "CE" else max(l["strike"] - S, 0)) - l["price"])
               for l in legs)


def today_pnl(legs: list, S: float, T: float) -> float:
    return sum(_q(l) * (bs(S, l["strike"], T, l["iv"], l["type"] == "CE") - l["price"]) for l in legs)


def slopes(legs: list) -> tuple[float, float]:
    """d(P&L)/dS far below every strike and far above it."""
    lo = sum(-_q(l) for l in legs if l["type"] == "PE")
    hi = sum(_q(l) for l in legs if l["type"] == "CE")
    return lo, hi


def metrics(legs: list, spot: float, iv: float, T: float) -> dict:
    """Max profit / loss at expiry (None = unlimited), breakevens, net premium, probability of profit."""
    ks = sorted({l["strike"] for l in legs})
    lo_s, hi_s = slopes(legs)
    pts = [0.0] + ks + [ks[-1] * 3]                  # payoff is linear between strikes: kinks + the ends are enough
    vals = [expiry_pnl(legs, s) for s in pts]
    mx, mn = max(vals), min(vals)
    max_profit = None if hi_s > 0 else round(mx, 2)  # rises forever above the top strike
    max_loss = None if hi_s < 0 else round(mn, 2)    # (below the lowest strike the price stops at 0: always finite)
    be = []
    for (a, va), (b, vb) in zip(zip(pts, vals), zip(pts[1:], vals[1:])):
        if va == 0 and (not be or abs(be[-1] - a) > 1e-9):
            be.append(a)
        elif va * vb < 0:
            be.append(a + (b - a) * (-va) / (vb - va))
    if vals[-1] == 0 and pts[-1] not in be:
        be.append(pts[-1])
    be = [round(x, 2) for x in be if 0 < x < ks[-1] * 3]
    net = -sum(_q(l) * l["price"] for l in legs)     # + credit received, − debit paid
    pop = prob_profit(legs, spot, iv, T)
    return {"max_profit": max_profit, "max_loss": max_loss, "breakevens": be, "net_premium": round(net, 2),
            "pop": pop, "unlimited_up": hi_s != 0, "slope_up": hi_s, "slope_down": lo_s}


def prob_profit(legs: list, spot: float, iv: float, T: float, n: int = 400) -> float | None:
    """P(P&L at expiry > 0) if the price ends lognormal with today's ATM IV (no drift). A rough guide only."""
    if not iv or T <= 0:
        return None
    sd = iv * math.sqrt(T)
    p = 0.0
    zs = [-5 + 10 * i / n for i in range(n + 1)]
    for a, b in zip(zs, zs[1:]):
        z = (a + b) / 2
        S = spot * math.exp(-0.5 * sd * sd + sd * z)
        if expiry_pnl(legs, S) > 0:
            p += _ncdf(b) - _ncdf(a)
    return round(p, 3)


def margin(legs: list, spot: float, symbol: str, seg: str, stock: bool, m: dict) -> dict:
    """Estimated margin to block (₹) and how it was worked out."""
    naked = {}
    for t in ("CE", "PE"):
        short = sum(-_q(l) for l in legs if l["type"] == t and _q(l) < 0)
        long_ = sum(_q(l) for l in legs if l["type"] == t and _q(l) > 0)
        naked[t] = max(short - long_, 0)
    if not naked["CE"] and not naked["PE"]:
        # the cash to buy everything back in the worst case = max loss + the credit already received (0 for a debit)
        need = max(-(m["max_loss"] or 0) + m["net_premium"], 0) if any(_q(l) < 0 for l in legs) else 0.0
        return {"margin": round(need, 2), "basis": "worst-case buy-back cost (risk is limited)" if need
                else "none — the premium paid is the most you can lose"}
    pct = SPAN_PCT.get("STOCK" if stock else symbol, SPAN_PCT.get(seg, 0.15))
    units = max(naked["CE"], naked["PE"])            # a short straddle / strangle is margined on one side
    need = pct * spot * units
    return {"margin": round(need, 2), "basis": f"≈{pct:.0%} of the underlying value per uncovered sold option "
                                                f"({units:g} units) — estimate; risk is NOT limited"}


def curves(legs: list, spot: float, T: float, iv: float, days_ahead: float = 0.0, n: int = 161) -> dict:
    ks = [l["strike"] for l in legs]
    span = max(3 * iv * math.sqrt(max(T, 1 / 365)) * spot, 1.6 * (max(ks + [spot]) - min(ks + [spot])), 0.02 * spot)
    lo, hi = max(spot - span, 0.01), spot + span
    xs = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
    for k in sorted(set(ks + [spot])):                # exact kinks / spot so the chart has sharp corners
        if lo < k < hi:
            xs.append(k)
    xs = sorted(set(round(x, 2) for x in xs))
    Tn = max(T - days_ahead / 365, 1e-6)
    return {"x": xs, "expiry": [round(expiry_pnl(legs, x), 2) for x in xs],
            "today": [round(today_pnl(legs, x, Tn), 2) for x in xs]}


def greeks(legs: list, S: float, T: float) -> dict:
    """Position Greeks: delta (₹ per 1 point), gamma, theta (₹ per day), vega (₹ per 1 IV point)."""
    d = g = th = v = 0.0
    for l in legs:
        sig, K, call, q = l["iv"], l["strike"], l["type"] == "CE", _q(l)
        sT = sig * math.sqrt(max(T, 1e-6))
        d1 = (math.log(S / K) + 0.5 * sig * sig * T) / sT
        pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
        d += q * (_ncdf(d1) if call else _ncdf(d1) - 1)
        g += q * pdf / (S * sT)
        th += q * (-S * pdf * sig / (2 * math.sqrt(max(T, 1e-6)))) / 365
        v += q * S * pdf * math.sqrt(max(T, 1e-6)) / 100
    return {"delta": round(d, 2), "gamma": round(g, 4), "theta": round(th, 2), "vega": round(v, 2)}


def analyse(legs: list, spot: float, T: float, atm_iv: float, symbol: str, seg: str, stock: bool,
            days_ahead: float = 0.0) -> dict:
    m = metrics(legs, spot, atm_iv, T)
    mg = margin(legs, spot, symbol, seg, stock, m)
    return {"metrics": m, **mg, "greeks": greeks(legs, spot, T), "curve": curves(legs, spot, T, atm_iv, days_ahead),
            "now_pnl": round(today_pnl(legs, spot, T), 2)}


# ───────────────────────────── live ─────────────────────────────
def quote(body: dict) -> dict:
    """body: {symbol, expiry, legs: [{strike, type, side, lots}], days_ahead} → priced legs + analysis."""
    from . import paper
    from .chain import implied_vol, years_left
    from .sync import now_ist
    symbol, expiry = body["symbol"], body["expiry"]
    if symbol == "BTC":
        raise ValueError("strategies are for NSE / BSE / MCX options")
    raw = body.get("legs") or []
    if not 1 <= len(raw) <= 6:
        raise ValueError("1 to 6 legs")
    cs = []
    for l in raw:
        c = paper.find(symbol, expiry, float(l["strike"]), l["type"])
        if c is None:
            raise ValueError(f"no contract {l['strike']:g} {l['type']} on {expiry}")
        cs.append(c)
    q, under = paper.quotes(cs, (symbol,))
    spot = under.get(symbol)
    if not spot:
        raise ValueError(f"no live price for {symbol}")
    now = now_ist()
    T = years_left(now, dt.date.fromisoformat(expiry), cs[0]["seg"])
    legs, ivs = [], []
    for l, c in zip(raw, cs):
        qc = q.get(c["token"]) or {}
        side = l["side"]
        px = paper.fill_price(side, qc)
        mid = ((qc["bid"] + qc["ask"]) / 2 if qc.get("bid") and qc.get("ask") else qc.get("ltp"))
        iv = implied_vol(mid, spot, c["strike"], T, c["type"] == "CE")
        legs.append({"strike": c["strike"], "type": c["type"], "side": side, "lots": int(l.get("lots") or 1), "lot": c["lot"],
                     "tsym": c["tsym"], "price": px, "bid": qc.get("bid"), "ask": qc.get("ask"), "ltp": qc.get("ltp"),
                     "iv": iv / 100 if iv else None})
        if iv and abs(c["strike"] - spot) / spot < 0.03:
            ivs.append(iv / 100)
    if any(l["price"] is None for l in legs):
        missing = ", ".join(l["tsym"] for l in legs if l["price"] is None)
        return {"error": f"no live quote for {missing}", "spot": spot}
    known = [l["iv"] for l in legs if l["iv"]]
    atm_iv = (sum(ivs) / len(ivs)) if ivs else (sum(known) / len(known) if known else 0.15)
    for l in legs:
        l["iv"] = l["iv"] or atm_iv                    # deep ITM below intrinsic etc.: use the ATM IV
    out = analyse(legs, spot, T, atm_iv, symbol, cs[0]["seg"], symbol not in paper.UNDER,
                  float(body.get("days_ahead") or 0))
    return {"symbol": symbol, "expiry": expiry, "spot": spot, "T_days": round(T * 365, 2), "atm_iv": round(atm_iv * 100, 1),
            "legs": [{**l, "iv": round(l["iv"] * 100, 1)} for l in legs], "at": now.isoformat(timespec="seconds"), **out}


def order(body: dict) -> dict:
    """Paper-trade a strategy: re-prices it live, takes the margin from our own estimate (not the browser's)."""
    from . import paper
    a = quote({**body, "days_ahead": 0})
    if a.get("error"):
        return {"status": "rejected", "reason": a["error"]}
    legs = [{"symbol": body["symbol"], "expiry": body["expiry"], "strike": l["strike"], "type": l["type"],
             "side": l["side"], "lots": l["lots"]} for l in a["legs"]]
    return paper.strategy_order("manual", {"name": body.get("name") or "Strategy", "legs": legs, "margin": a["margin"],
                                           "stop_rs": body.get("stop_rs"), "target_rs": body.get("target_rs"),
                                           "note": body.get("note")})
