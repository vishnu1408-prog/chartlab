"""Swing picks: 2–6 week long-only trades in liquid NSE stocks (cash / delivery). PAPER TRADING.

Research (indicators/tests/swing_data.py, swing_model.py, 2026-10-15): every Friday the 300 most-traded stocks are
ranked by a gradient-boosting model on 19 price / volume features (each as a percentile among the 300); target =
next-4-week return rank. Walk-forward 2014–2026: rank-IC +0.07, positive 12 of 13 years, weaker since 2022.
Plan chosen on 2014–21 only: each Friday buy the 2 best-ranked stocks not held (max 8, equal ₹ size) at Monday's
open; sell at the open after 30 trading days (~6 weeks), the open after a close ≤ entry − 10 %, or (user's choice,
20 Nov 2026) the open after a close ≥ entry + 25 % (target — tested ≈ the same as holding). Trailing stop (20 Nov
2026): from a close ≥ +10 %, stop = max(breakeven, highest close − 10 %); 2014-21 +25 %/yr vs +22 %, 2022-26 +19.5 %
vs +17 % (data/swing_research/swing_trail.py). Costs 0.35 % per side.
Backtest 2022 → Oct 2026: +10 %/yr, max drawdown −41 %, 2024 −10 %, 2025 −8 % ≈ NIFTY 500 (+8 %) → weak edge.

Data: NSE CM bhavcopy (every stock, daily, published ~18:00–19:00 IST) + corporate actions from the PR archive.
Prices are split / bonus / demerger / dividend adjusted. Files in data/swing/:
  work.parquet   last ~420 trading days, all EQ/BE/BZ stocks (adjusted)  — features need 250 days
  panel.parquet  full history 2010 → (only for `python -m app.swing train`)
  corp_actions.csv, model.pkl, backtest.json; the paper portfolio, events and picks live in data/chartlab.db (swing_*)
"""
from __future__ import annotations

import datetime as dt
import io
import json
import logging
import pickle
import re
import threading
import time
import zipfile

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger("chartlab.swing")
DIR = config.DATA_DIR / "swing"
UNIVERSE, K_NEW, SLOTS, HOLD, STOP, TARGET, COST = 300, 2, 8, 30, 0.10, 0.25, 0.0035
TRAIL_ACT, TRAIL_DIST = 0.10, 0.10            # trailing stop: active from +10 % (closing basis), 10 % below highest close
CAPITAL = 150_000
H_FWD = 20
WORK_DAYS = 420
STD = np.array([1 / 2, 1 / 3, 2 / 3, 1 / 4, 3 / 4, 1 / 5, 2 / 5, 1 / 10, 1 / 6])
status: dict = {"state": "idle"}
_lock = threading.Lock()

LABEL = {
    "r5": ("1-week return", "pct"), "r20": ("1-month return", "pct"), "r60": ("3-month return", "pct"),
    "r120": ("6-month return", "pct"), "mom12_1": ("12-month trend (skipping last month)", "pct"),
    "hi52": ("distance below 52-week high", "pct"), "lo52": ("rise from 52-week low", "pct"),
    "dma50": ("price vs 50-day average", "pct"), "dma200": ("price vs 200-day average", "pct"),
    "vol20": ("1-month volatility (daily)", "pct"), "vol60": ("3-month volatility (daily)", "pct"),
    "atr": ("average daily range", "pct"), "maxret20": ("biggest 1-day jump this month", "pct"),
    "volu": ("volume: last month vs last 6 months", "x"), "beta": ("beta to the market", "num"),
    "idio": ("stock-specific volatility", "pct"), "rsi": ("RSI(14)", "rsi"),
    "updays": ("up days in the last month", "share"), "range_atr": ("1-month range ÷ daily range", "num"),
}


# ───────────────────────────── NSE downloads ─────────────────────────────
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Referer": "https://www.nseindia.com/"}
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
SER = ("EQ", "BE", "BZ")


def _get(url: str) -> bytes | None:
    import requests
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=40)
            if r.status_code == 404:
                return None
            if r.ok:
                return r.content
        except Exception as e:
            log.warning("swing download %s: %s", url[-40:], e)
        time.sleep(2 + 3 * attempt)
    return None


def fetch_cm(d: dt.date) -> pd.DataFrame | None:
    """One day's cash-market bhavcopy (None = no file: holiday or not published yet)."""
    old = (f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{d.year}/{MON[d.month - 1]}/"
           f"cm{d.day:02d}{MON[d.month - 1]}{d.year}bhav.csv.zip")
    new = f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    for u in ([new, old] if d >= dt.date(2024, 7, 8) else [old, new]):
        raw = _get(u)
        if raw:
            return parse_cm(raw, d)
    return None


def parse_cm(raw: bytes, d: dt.date) -> pd.DataFrame:
    z = zipfile.ZipFile(io.BytesIO(raw))
    df = pd.read_csv(z.open(z.namelist()[0]), low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    if "SYMBOL" in df.columns:
        df["SERIES"] = df.SERIES.astype(str).str.strip()
        df = df[df.SERIES.isin(SER)]
        out = pd.DataFrame(dict(sym=df.SYMBOL, series=df.SERIES, open=df.OPEN, high=df.HIGH, low=df.LOW,
                                close=df.CLOSE, prev=df.PREVCLOSE, value=df.TOTTRDVAL, isin=df.get("ISIN")))
    else:
        df = df[df.SctySrs.astype(str).str.strip().isin(SER)]
        out = pd.DataFrame(dict(sym=df.TckrSymb, series=df.SctySrs.astype(str).str.strip(), open=df.OpnPric,
                                high=df.HghPric, low=df.LwPric, close=df.ClsPric, prev=df.PrvsClsgPric, value=df.TtlTrfVal,
                                isin=df.ISIN))
    out = out[~out["isin"].astype(str).str.startswith("INF")].drop(columns="isin")   # ETFs / fund units are not stocks
    out.insert(0, "date", pd.Timestamp(d))
    out["_o"] = out.series.map({"EQ": 0, "BE": 1, "BZ": 2})
    return out.sort_values(["sym", "_o"]).drop_duplicates("sym").drop(columns="_o").reset_index(drop=True)


def fetch_ca(d: dt.date) -> pd.DataFrame | None:
    """Corporate actions listed in that day's PR archive (Bc file): sym, series, ex_dt, purpose."""
    raw = _get(f"https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{d:%d%m%y}.zip")
    if not raw:
        return None
    z = zipfile.ZipFile(io.BytesIO(raw))
    n = [x for x in z.namelist() if x.lower().startswith("bc") and x.lower().endswith(".csv")]
    if not n:
        return None
    x = pd.read_csv(z.open(n[0]), dtype=str, skipinitialspace=True, on_bad_lines="skip")
    x.columns = [c.strip() for c in x.columns]
    x = x[["SERIES", "SYMBOL", "EX_DT", "PURPOSE"]].apply(lambda c: c.str.strip())
    x.columns = ["series", "sym", "ex_dt", "purpose"]
    x["ex_dt"] = pd.to_datetime(x.ex_dt, format="%d/%m/%Y", errors="coerce")
    return x.dropna(subset=["ex_dt"])


# ───────────────────────────── corporate-action adjustment ─────────────────────────────
def ca_factor(purpose: str, prev: float):
    """Price multiplier on the ex-date implied by a corporate-action text. None = no price change.
    "OPEN" = demerger / scheme: use the ex-date open ÷ previous close."""
    p = str(purpose).upper()
    if re.search(r"DEMERG|ARRANGEMENT|CAPITAL REDUCTION|SCHEME", p):
        return "OPEN"
    k = 1.0
    m = re.search(r"BO?N(?:US)?\D{0,3}?(\d+)\s*:\s*(\d+)", p)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0:
            k *= b / (a + b)
    m = re.search(r"(?:SPLIT|SPLT|SPL|SUB-?DIV)[^0-9]*?(\d+(?:\.\d+)?)\D+?(\d+(?:\.\d+)?)", p)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > b > 0:
            k *= b / a
    if k < 1:
        return k
    if "DIV" in p and prev > 0:
        amt = [float(x) for x in re.findall(r"(?:RS|RE)\.?\s*-?\s*(\d+(?:\.\d+)?)", p)]
        amt = [x for x in amt if x < prev * 0.3]
        if amt and sum(amt) / prev >= 0.005:
            return (prev - sum(amt)) / prev
    return None


def day_factors(day: pd.DataFrame, ca: pd.DataFrame) -> dict:
    """{sym: multiplier for all earlier prices} for one new bhavcopy day (one price change + one dividend at most)."""
    d = day.date.iloc[0]
    r_open = (day.open / day.prev).to_numpy()
    r_close = (day.close / day.prev).to_numpy()
    idx = {s: i for i, s in enumerate(day.sym)}
    cand: dict = {}
    for r in ca[ca.ex_dt == d].itertuples():
        i = idx.get(r.sym)
        if i is None:
            continue
        k = ca_factor(r.purpose, day.prev.iat[i])
        if k is not None:
            cand.setdefault(i, []).append(r_open[i] if k == "OPEN" else k)
    out = {}
    for i, ks in cand.items():
        price = [k for k in ks if k <= 0.9]
        div = [k for k in ks if k > 0.9]
        if price:
            k = min(price, key=lambda k: abs(r_open[i] / k - 1))
            if abs(r_open[i] / k - 1) < 0.25:
                out[day.sym.iat[i]] = float(k)
        elif div:
            out[day.sym.iat[i]] = float(min(div))
    for i in range(len(day)):                            # unrecorded bonus / split
        s = day.sym.iat[i]
        if s in out or not (r_close[i] <= 0.62 and r_open[i] <= 0.62):
            continue
        j = np.argmin(np.abs(r_close[i] / STD - 1))
        if abs(r_close[i] / STD[j] - 1) < 0.06:
            out[s] = float(STD[j])
    return out


# ───────────────────────────── features (same as the research script) ─────────────────────────────
def wide(p: pd.DataFrame) -> dict:
    W = {c: p.pivot(index="date", columns="sym", values=c).astype("float32") for c in ("open", "high", "low", "close", "value")}
    W["eq"] = p.assign(eq=(p.series == "EQ").astype("float32")).pivot(index="date", columns="sym", values="eq")
    return W


def features(W: dict):
    C, Hh, L, V = W["close"], W["high"], W["low"], W["value"]
    Cf = C.ffill(limit=5)
    ret = Cf.pct_change(fill_method=None)
    n250 = C.notna().astype("float32").rolling(250, min_periods=1).sum()
    medv = V.rolling(60, min_periods=40).median()
    mkt = ret.where(n250 >= 230).mean(axis=1)
    mkt_idx = (1 + mkt.fillna(0)).cumprod()
    TR = np.maximum(np.maximum(Hh - L, (Hh - Cf.shift()).abs()), (L - Cf.shift()).abs())
    atr = TR.rolling(14, min_periods=10).mean()
    up = ret.clip(lower=0).rolling(14, min_periods=10).mean()
    dn = (-ret.clip(upper=0)).rolling(14, min_periods=10).mean()
    beta = ret.rolling(60, min_periods=40).cov(mkt).div(mkt.rolling(60, min_periods=40).var(), axis=0)
    resid = ret - beta.mul(mkt, axis=0)
    F = {
        "r5": Cf / Cf.shift(5) - 1, "r20": Cf / Cf.shift(20) - 1, "r60": Cf / Cf.shift(60) - 1,
        "r120": Cf / Cf.shift(120) - 1, "mom12_1": Cf.shift(20) / Cf.shift(250) - 1,
        "hi52": Cf / Hh.rolling(250, min_periods=200).max() - 1, "lo52": Cf / L.rolling(250, min_periods=200).min() - 1,
        "dma50": Cf / Cf.rolling(50, min_periods=40).mean() - 1, "dma200": Cf / Cf.rolling(200, min_periods=160).mean() - 1,
        "vol20": ret.rolling(20, min_periods=15).std(), "vol60": ret.rolling(60, min_periods=40).std(),
        "atr": atr / Cf, "maxret20": ret.rolling(20, min_periods=15).max(),
        "volu": V.rolling(20, min_periods=15).mean() / V.rolling(120, min_periods=80).mean(),
        "beta": beta, "idio": resid.rolling(60, min_periods=40).std(), "rsi": up / (up + dn),
        "updays": (ret > 0).astype("float32").where(C.notna()).rolling(20, min_periods=15).mean(),
        "range_atr": (Hh.rolling(20, min_periods=15).max() - L.rolling(20, min_periods=15).min()) / atr,
    }
    M = pd.DataFrame({"m_r20": mkt_idx / mkt_idx.shift(20) - 1, "m_r60": mkt_idx / mkt_idx.shift(60) - 1,
                      "m_vol20": mkt.rolling(20).std(),
                      "m_above200": (Cf > Cf.rolling(200, min_periods=160).mean()).where(n250 >= 230).mean(axis=1),
                      "m_trend": (mkt_idx > mkt_idx.rolling(200).mean()).astype(float)})
    return F, M, medv, n250


def rows_for(W, F, M, medv, n250, d) -> pd.DataFrame | None:
    """Feature rows of the day-d universe (top 300 by traded value)."""
    C = W["close"]
    i = C.index.get_loc(d)
    elig = (W["eq"].iloc[i] == 1) & (n250.iloc[i] >= 230) & (C.iloc[i] >= 30) & medv.iloc[i].notna()
    syms = medv.iloc[i][elig].nlargest(UNIVERSE).index
    if len(syms) < 100:
        return None
    X = pd.DataFrame({k: v.loc[d, syms] for k, v in F.items()})
    R = X.rank(pct=True)
    R.columns = ["p_" + c for c in X.columns]
    X = pd.concat([X, R], axis=1)
    for k in M.columns:
        X[k] = M.loc[d, k]
    X["date"], X["sym"], X["close"] = d, syms, C.loc[d, syms].to_numpy()
    return X


# ───────────────────────────── model ─────────────────────────────
def train(panel_path=None) -> dict:
    """Final model on all history (heavy: ~4 GB RAM, several minutes). Run: docker exec chartlab python -m app.swing train"""
    from sklearn.ensemble import HistGradientBoostingRegressor
    p = pd.read_parquet(panel_path or DIR / "panel.parquet")
    v = p.pivot(index="date", columns="sym", values="value").astype("float32")
    keep = v.columns[(v.rolling(60, min_periods=40).median().rank(axis=1, ascending=False) <= 450).any()]
    del v
    p = p[p.sym.isin(keep)]
    W = wide(p)
    del p
    F, M, medv, n250 = features(W)
    idx = W["close"].index
    s = pd.Series(idx, index=idx)
    iso = idx.isocalendar()
    fridays = sorted(s.groupby([iso.year.values, iso.week.values]).max())
    Of, Cl = W["open"].to_numpy(), W["close"].ffill().to_numpy()
    rows = []
    for d in fridays:
        if d < pd.Timestamp("2011-01-01"):
            continue
        X = rows_for(W, F, M, medv, n250, d)
        i = idx.get_loc(d)
        if X is None or i + 1 + H_FWD >= len(idx):
            continue
        cols = [W["close"].columns.get_loc(s) for s in X.sym]
        e = np.where(np.isnan(Of[i + 1, cols]), Cl[i, cols], Of[i + 1, cols])
        x = np.where(np.isnan(Of[i + 1 + H_FWD, cols]), Cl[i + H_FWD, cols], Of[i + 1 + H_FWD, cols])
        X["fwd"] = x / e - 1
        rows.append(X)
    T = pd.concat(rows, ignore_index=True)
    T["y"] = T.groupby("date").fwd.rank(pct=True) - 0.5
    feats = [c for c in T.columns if c.startswith("p_") or c.startswith("m_")]
    m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.04, max_leaf_nodes=31, min_samples_leaf=400,
                                      l2_regularization=1.0, random_state=0).fit(T[feats], T.y)
    import sklearn
    M_ = {"model": m, "feats": feats, "meta": {"trained_until": str(T.date.max().date()), "rows": int(len(T)),
                                               "created": dt.datetime.now().isoformat(timespec="seconds"),
                                               "sklearn": sklearn.__version__}}
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / "model.pkl").write_bytes(pickle.dumps(M_))
    log.info("swing model trained: %s", M_["meta"])
    return M_["meta"]


_model_cache: dict = {}


def load_model():
    p = DIR / "model.pkl"
    if not p.exists():
        return None
    mt = p.stat().st_mtime
    if _model_cache.get("mt") != mt:
        _model_cache.update(mt=mt, m=pickle.loads(p.read_bytes()))
    return _model_cache["m"]


def _fmt(k: str, v: float) -> str:
    kind = LABEL[k][1]
    if not np.isfinite(v):
        return "n/a"
    if kind == "pct":
        return f"{100 * v:+.1f}%" if k not in ("vol20", "vol60", "atr", "idio") else f"{100 * v:.1f}%"
    if kind == "x":
        return f"{v:.2f}×"
    if kind == "rsi":
        return f"{100 * v:.0f}"
    if kind == "share":
        return f"{100 * v:.0f}%"
    return f"{v:.2f}"


def explain(M_, X: pd.DataFrame, rows: list, top: int = 3) -> dict:
    """Why the model likes a stock: set each feature to the median stock (percentile 0.5) and see how far the score
    drops. Returns {sym: {"for": [...], "against": [...]}} in plain words."""
    feats = M_["feats"]
    out = {}
    base = M_["model"].predict(X.loc[rows, feats])
    for n, r in enumerate(rows):
        x0 = X.loc[[r], feats]
        Z = pd.concat([x0] * len(LABEL), ignore_index=True)
        names = list(LABEL)
        for j, k in enumerate(names):
            Z.loc[j, "p_" + k] = 0.5
        eff = base[n] - M_["model"].predict(Z)
        order = np.argsort(-eff)

        def say(j):
            k = names[j]
            pc = float(X.at[r, "p_" + k])
            pos = f"top {max(1, round(100 * (1 - pc)))}%" if pc >= 0.5 else f"bottom {max(1, round(100 * pc))}%"
            return {"what": LABEL[k][0], "value": _fmt(k, float(X.at[r, k])), "rank": pos, "effect": round(float(eff[j]), 4)}
        out[X.at[r, "sym"]] = {"for": [say(j) for j in order[:top] if eff[j] > 0.002],
                               "against": [say(j) for j in order[::-1][:2] if eff[j] < -0.002]}
    return out


def reason_text(ex: dict) -> str:
    f = "; ".join(f"{a['what']} {a['value']} ({a['rank']} of the 300)" for a in ex.get("for", []))
    a = "; ".join(f"{a['what']} {a['value']}" for a in ex.get("against", []))
    return f + (f" — against it: {a}" if a else "")


# ───────────────────────────── daily update + paper portfolio ─────────────────────────────
def _read_state() -> dict:
    from . import db
    return db.swing_load() or {"live_from": None, "positions": [], "pending": [], "closed": [], "events": [], "decided": []}


def _write_state(s: dict):
    from . import db
    db.swing_save(s)


def _event(s: dict, kind: str, sym: str, text: str, day):
    s["events"].append({"id": f"{day}-{kind}-{sym}", "day": str(day)[:10], "kind": kind, "sym": sym, "text": text,
                        "at": dt.datetime.now().isoformat(timespec="seconds")})


def _load_work() -> pd.DataFrame:
    return pd.read_parquet(DIR / "work.parquet")


def add_day(work: pd.DataFrame, day: pd.DataFrame, ca: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Append one bhavcopy day; adjust earlier prices of stocks with a corporate action on that day."""
    fac = day_factors(day, ca)
    if fac:
        m = work.sym.isin(list(fac))
        k = work.loc[m, "sym"].map(fac)
        for c in ("open", "high", "low", "close"):
            work.loc[m, c] = work.loc[m, c] * k
    work = pd.concat([work, day.drop(columns="prev")], ignore_index=True)
    keep = sorted(work.date.unique())[-WORK_DAYS:]
    return work[work.date >= keep[0]].reset_index(drop=True), fac


def _is_week_end(d: pd.Timestamp, days: list) -> bool:
    """d is the last trading day of its ISO week: a Friday, or the week already moved on."""
    if d.weekday() == 4:
        return True
    later = [x for x in days if x > d]
    return bool(later) and later[0].isocalendar()[1] != d.isocalendar()[1]


def step_portfolio(s: dict, W: dict, d: pd.Timestamp, fac: dict):
    """Process trading day d (its bhavcopy just arrived): fills at d's open, then end-of-day exit checks."""
    O, C = W["open"], W["close"]

    def px(tab, sym):
        try:
            v = float(tab.at[d, sym])
            return v if np.isfinite(v) and v > 0 else None
        except KeyError:
            return None

    for p in s["positions"]:                       # corporate action today → keep entry / stop in today's terms
        if p["sym"] in fac:
            for k in ("entry", "stop", "target", "hc"):
                if p.get(k) is not None:
                    p[k] = round(p[k] * fac[p["sym"]], 2)
            p["qty"] = int(round(p["qty"] / fac[p["sym"]]))
    # 1) exits decided yesterday fill at today's open
    still = []
    for p in s["positions"]:
        if p.get("exit_due"):
            o = px(O, p["sym"]) or p.get("last") or p["entry"]
            ret = o / p["entry"] * (1 - COST) / (1 + COST) - 1
            pnl = p["qty"] * (o * (1 - COST) - p["entry"] * (1 + COST))
            s["closed"].append({**p, "exit": round(o, 2), "exit_day": str(d.date()), "ret": round(ret, 4), "pnl": round(pnl)})
            _event(s, "SOLD", p["sym"], f"Paper-sold {p['sym']} at {o:.2f} ({p['exit_due']}): {100 * ret:+.1f}% after costs, "
                                        f"₹{pnl:+,.0f}", d.date())
        else:
            still.append(p)
    s["positions"] = still
    # 2) entries decided last week fill at today's open
    if s["pending"]:
        for q in s["pending"]:
            if len(s["positions"]) >= SLOTS:
                break
            o = px(O, q["sym"])
            if o is None:
                _event(s, "SKIP", q["sym"], f"{q['sym']} did not trade today — buy skipped", d.date())
                continue
            qty = max(1, int(CAPITAL / SLOTS // o))
            s["positions"].append({"sym": q["sym"], "entry": round(o, 2), "entry_day": str(d.date()), "qty": qty,
                                   "stop": round(o * (1 - STOP), 2), "target": round(o * (1 + TARGET), 2),
                                   "days": 0, "reason": q.get("reason", ""), "score": q.get("score")})
            _event(s, "BOUGHT", q["sym"], f"Paper-bought {qty} × {q['sym']} at {o:.2f}; stop {o * (1 - STOP):.2f} (−10% on "
                                          f"a closing basis), target {o * (1 + TARGET):.2f}, time exit after "
                                          f"{HOLD} trading days", d.date())
        s["pending"] = []
    # 3) end of day: mark and flag exits for tomorrow's open
    for p in s["positions"]:
        p["days"] += 1
        c = px(C, p["sym"])
        if c is None:
            continue
        p["last"], p["last_day"] = round(c, 2), str(d.date())
        p["ret"] = round(c / p["entry"] - 1, 4)
        if p.get("exit_due"):
            continue
        # trailing stop (user's rule, 20 Nov 2026; tested in data/swing_research/swing_trail.py): once a close is
        # ≥ entry + 10 %, the stop rises to max(breakeven after costs, highest close − 10 %) and never moves down
        p["hc"] = round(max(p.get("hc") or p["entry"], c), 2)   # highest close since entry (from the rule's start)
        if p["hc"] >= p["entry"] * (1 + TRAIL_ACT):
            new = round(max(p["entry"] * (1 + 2 * COST), p["hc"] * (1 - TRAIL_DIST)), 2)
            if new > p["stop"]:
                if not p.get("trailing"):
                    _event(s, "TRAIL", p["sym"], f"{p['sym']} is up {100 * (c / p['entry'] - 1):.0f}% — stop raised to "
                                                 f"{new:.2f} (no loss possible on a close below it)", d.date())
                p["stop"], p["trailing"] = new, True
        if c <= p["stop"]:
            p["exit_due"] = "trailing stop" if p.get("trailing") else "stop −10%"
            _event(s, "SELL", p["sym"], f"SELL {p['sym']} at tomorrow's open — closed at {c:.2f}, below the stop "
                                        f"{p['stop']:.2f}", d.date())
        elif p["days"] >= HOLD:
            p["exit_due"] = f"{HOLD} days done"
            _event(s, "SELL", p["sym"], f"SELL {p['sym']} at tomorrow's open — {HOLD} trading days (~6 weeks) are up; "
                                        f"now {100 * p['ret']:+.1f}%", d.date())
        elif c >= p["target"]:                      # user's rule (20 Nov 2026): book at the +25 % target
            p["target_hit"] = True
            p["exit_due"] = "target +25%"
            _event(s, "SELL", p["sym"], f"SELL {p['sym']} at tomorrow's open — closed at {c:.2f}, at / above the "
                                        f"+25% target {p['target']:.2f}", d.date())


def rank_day(W, F, M, medv, n250, d) -> dict | None:
    M_ = load_model()
    X = rows_for(W, F, M, medv, n250, d)
    if M_ is None or X is None:
        return None
    X = X.reset_index(drop=True)
    X["score"] = M_["model"].predict(X[M_["feats"]])
    X = X.sort_values("score", ascending=False).reset_index(drop=True)
    top = list(range(15))
    ex = explain(M_, X, top)
    rows = []
    for i in top:
        r = X.loc[i]
        e = ex[r.sym]
        rows.append({"rank": i + 1, "sym": r.sym, "close": round(float(r.close), 2), "score": round(float(r.score), 4),
                     "stop": round(float(r.close) * (1 - STOP), 2), "target": round(float(r.close) * (1 + TARGET), 2),
                     "qty": max(1, int(CAPITAL / SLOTS // float(r.close))), "why": e, "reason": reason_text(e),
                     "r20": round(float(r.r20), 4), "r120": round(float(r.r120), 4), "hi52": round(float(r.hi52), 4)})
    return {"day": str(d.date()), "market": {k: round(float(M.loc[d, k]), 4) for k in M.columns},
            "model": M_["meta"], "top": rows}


def update(force_rank: bool = False) -> dict:
    """Bring work.parquet up to date with NSE files, run the paper portfolio day by day, make Friday picks."""
    with _lock:
        status["state"] = "updating"
        try:
            return _update(force_rank)
        finally:
            status["state"] = "idle"


def _update(force_rank: bool) -> dict:
    from .sync import now_ist
    work = _load_work()
    ca = pd.read_csv(DIR / "corp_actions.csv", parse_dates=["ex_dt"])
    s = _read_state()
    now = now_ist()
    last = work.date.max().date()
    new_days = []
    d = last + dt.timedelta(days=1)
    while d <= now.date():
        if d.weekday() < 5 and (d < now.date() or now.hour * 60 + now.minute >= 18 * 60):
            day = fetch_cm(d)
            if day is not None and len(day) > 500:
                c2 = fetch_ca(d)
                if c2 is not None:
                    ca = pd.concat([ca, c2]).drop_duplicates(["sym", "ex_dt", "purpose"])
                    ca.to_csv(DIR / "corp_actions.csv", index=False)
                work, fac = add_day(work, day, ca)
                new_days.append((pd.Timestamp(d), fac))
            elif d < now.date() - dt.timedelta(days=3):
                pass                                            # holiday
        d += dt.timedelta(days=1)
    if new_days:
        work.to_parquet(DIR / "work.parquet", index=False)
    status["last_day"] = str(work.date.max().date())
    from . import db
    if not new_days and not force_rank and db.pick_exists(str(work.date.max().date())):
        return status
    W = wide(work)
    F, M, medv, n250 = features(W)
    days = list(W["close"].index)
    if s["live_from"] is None:
        s["live_from"] = str(days[-1].date())
    for d, fac in new_days:
        step_portfolio(s, W, d, fac)
    # rank on the latest day; Friday (week end) → official picks for the next open
    d = days[-1]
    pk = rank_day(W, F, M, medv, n250, d)
    if pk:
        wk = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
        pk["official"] = _is_week_end(d, days) and d.weekday() == 4
        if pk["official"] and wk not in s["decided"]:
            held = {p["sym"] for p in s["positions"]}                     # as in the backtest: skip anything held
            free = SLOTS - sum(1 for p in s["positions"] if not p.get("exit_due"))
            buys = [r for r in pk["top"] if r["sym"] not in held][:min(K_NEW, max(free, 0))]
            s["pending"] = [{"sym": r["sym"], "score": r["score"], "reason": r["reason"], "close": r["close"]} for r in buys]
            s["decided"].append(wk)
            for r in buys:
                _event(s, "BUY", r["sym"], f"BUY {r['sym']} at Monday's open (Friday close {r['close']:.2f}); about "
                                           f"{r['qty']} shares for ₹{CAPITAL // SLOTS:,}. Stop {r['stop']:.2f} "
                                           f"(close below → sell), target {r['target']:.2f}, hold up to 6 weeks. "
                                           f"Why: {r['reason']}", d.date())
            if not buys:
                _event(s, "INFO", "-", "No new buys this week (all 8 slots are full).", d.date())
        pk["pending"] = s["pending"]
        db.pick_put(str(d.date()), json.loads(json.dumps(pk, default=str)))
    _write_state(s)
    return status


def eq_token(sym: str):
    """Angel token of an NSE equity (any stock, not only ChartLab's chart symbols)."""
    from .sources import Angel
    if Angel._tokens is None or Angel._tokens_day != dt.date.today():
        Angel.token("NIFTY 50")                              # builds the full scrip-master map
    return (Angel._tokens or {}).get(("NSE", sym.upper()))


_intraday_cache: dict = {}                                   # sym → (day, candles): Angel may refuse a request
                                                             # when the live loop just used the quota → keep last good


def chart(sym: str) -> dict:
    """Daily candles (adjusted, ~20 months) + today's 5-minute candles and live price from Angel when available,
    + the paper position / ranking for the levels drawn on the chart."""
    from .sources import Angel
    from .sync import now_ist
    w = _load_work()
    d = w[w.sym == sym].sort_values("date")
    if d.empty:
        raise KeyError(sym)
    c = d.set_index("date").close
    daily = [{"time": str(t.date()), "open": round(float(r.open), 2), "high": round(float(r.high), 2),
              "low": round(float(r.low), 2), "close": round(float(r.close), 2)} for t, r in d.set_index("date").iterrows()]
    ma = {n: [{"time": str(t.date()), "value": round(float(v), 2)} for t, v in c.rolling(n).mean().dropna().items()]
          for n in (50, 200)}
    out = {"sym": sym, "daily": daily, "ma50": ma[50], "ma200": ma[200], "intraday": [], "ltp": None, "live": False}
    s = _read_state()
    out["position"] = next((p for p in s["positions"] if p["sym"] == sym), None)
    out["pending"] = next((p for p in s["pending"] if p["sym"] == sym), None)
    out["closed"] = [p for p in s["closed"] if p["sym"] == sym][-5:]
    from . import db
    pk = db.pick_latest()
    if pk:
        out["rank"] = next((r for r in pk["top"] if r["sym"] == sym), None)
    if config.OFFLINE or not Angel.available():
        return out
    tok = eq_token(sym)
    api = Angel.api()
    if tok is None or api is None:
        return out
    now = now_ist()
    try:
        q = Angel.quotes({"NSE": [tok]}, "FULL").get(str(tok)) or {}
        if q.get("ltp"):
            pc = float(q.get("close") or 0) or None                 # FULL quote: "close" = previous close
            out.update(ltp=float(q["ltp"]), live=True, prev_close=pc,
                       change_pct=round(100 * (float(q["ltp"]) / pc - 1), 2) if pc else None)
            if out["position"]:
                out["position"]["ltp"] = float(q["ltp"])
        if now.weekday() < 5 and now.hour * 60 + now.minute >= 9 * 60 + 15 and not Angel.cooling():
            a = now.replace(hour=9, minute=15, second=0, microsecond=0)
            rows = Angel._get(api, attempts=1, params={"exchange": "NSE", "symboltoken": tok, "interval": "FIVE_MINUTE",
                                                      "fromdate": a.strftime("%Y-%m-%d %H:%M"),
                                                      "todate": now.strftime("%Y-%m-%d %H:%M")})
            if not rows and _intraday_cache.get(sym, (None,))[0] == now.date():
                rows = _intraday_cache[sym][1]
            elif rows:
                _intraday_cache[sym] = (now.date(), rows)
            for r in rows or []:
                t = pd.Timestamp(r[0])
                if t.tzinfo is not None:
                    t = t.tz_convert("Asia/Kolkata").tz_localize(None)
                # IST wall-clock time sent as if it were UTC seconds (ChartLab convention → IST labels on the chart)
                out["intraday"].append({"time": int((t - pd.Timestamp(0)).total_seconds()),
                                        "open": r[1], "high": r[2], "low": r[3], "close": r[4]})
    except Exception as e:
        log.warning("swing chart live %s: %s", sym, e)
    return out


_ltp_cache: dict = {"t": 0.0, "q": {}, "o": {}}


def live_prices(syms: list) -> dict:
    """{sym: ltp} from Angel quotes, cached 25 s (quotes have their own rate limit, separate from candles)."""
    from .sources import Angel
    from .sync import now_ist
    if config.OFFLINE or not syms or not Angel.available():
        return {}
    t = now_ist()
    m = t.hour * 60 + t.minute
    if t.weekday() >= 5 or not (9 * 60 + 15 <= m <= 15 * 60 + 35):
        return {}
    if time.time() - _ltp_cache["t"] > 25 or not set(syms) <= set(_ltp_cache["q"]):
        toks = {sym: eq_token(sym) for sym in syms}
        q = Angel.quotes({"NSE": [t_ for t_ in toks.values() if t_]}, "OHLC")
        ok = {sym: q[str(tk)] for sym, tk in toks.items() if tk and str(tk) in q and q[str(tk)].get("ltp")}
        _ltp_cache.update(t=time.time(), q={sym: float(x["ltp"]) for sym, x in ok.items()},
                          o={sym: float(x["open"]) for sym, x in ok.items() if x.get("open")})
    return {k: v for k, v in _ltp_cache["q"].items() if k in syms}


def kickstart() -> dict:
    """Start the paper portfolio NOW (instead of waiting for Friday): buy the latest ranking's top K_NEW at the live
    price. Only when nothing was ever traded. Later weeks follow the tested Friday rule."""
    with _lock:
        s = _read_state()
        if s["positions"] or s["closed"] or s["pending"]:
            return {"started": False, "why": "portfolio already running"}
        from . import db
        pk = db.pick_latest()
        if not pk:
            return {"started": False, "why": "no ranking yet"}
        from .sync import now_ist
        now = now_ist()
        top = [r["sym"] for r in pk["top"][:K_NEW]]
        ltp = live_prices(top)
        if len(ltp) < len(top):
            return {"started": False, "why": "no live prices (market closed or Angel not connected)"}
        for r in pk["top"][:K_NEW]:
            o = ltp[r["sym"]]
            qty = max(1, int(CAPITAL / SLOTS // o))
            s["positions"].append({"sym": r["sym"], "entry": round(o, 2), "entry_day": str(now.date()), "qty": qty,
                                   "stop": round(o * (1 - STOP), 2), "target": round(o * (1 + TARGET), 2), "days": 0,
                                   "reason": r["reason"], "score": r["score"], "entry_note": f"start-up buy at live price {now:%H:%M}"})
            _event(s, "BOUGHT", r["sym"], f"Paper-bought {qty} × {r['sym']} at the live price {o:.2f} ({now:%H:%M}) to start "
                                          f"the portfolio (rank #{r['rank']} on {pk['day']}). Stop {o * (1 - STOP):.2f} on a "
                                          f"closing basis, target {o * (1 + TARGET):.2f}, time exit after {HOLD} "
                                          f"trading days. Why: {r['reason']}", now.date())
        s["live_from"] = str(now.date())
        _write_state(s)
        return {"started": True, "bought": top}


def snapshot() -> dict:
    """Everything the dashboard shows."""
    s = _read_state()
    from . import db
    latest, official = db.pick_latest(), db.pick_latest(official=True)
    try:
        live = live_prices([p["sym"] for p in s["positions"]] + [q["sym"] for q in s["pending"]])
    except Exception as e:
        log.warning("swing live prices: %s", e)
        live = {}
    # Display only: exits / buys due "at the next open" have filled at TODAY's open; the portfolio records them in
    # the evening run (official NSE open). Show the provisional fill from today's live quote meanwhile.
    opens = {k: v for k, v in _ltp_cache.get("o", {}).items() if k in live}
    for p in s["positions"]:
        o = opens.get(p["sym"]) if p.get("exit_due") else None
        if o:
            p["filled_today"] = o
            p["fill_ret"] = round(o / p["entry"] * (1 - COST) / (1 + COST) - 1, 4)
            p["fill_pnl"] = round(p["qty"] * (o * (1 - COST) - p["entry"] * (1 + COST)))
    for q in s["pending"]:
        o = opens.get(q["sym"])
        if o:
            q["filled_today"] = o
            q["fill_qty"] = max(1, int(CAPITAL / SLOTS // o))
    from .sync import now_ist
    today = str(now_ist().date())
    for p in s["positions"]:                     # provisional rows / alerts, same ids as tonight's official ones
        if p.get("filled_today"):
            o = p["filled_today"]
            s["closed"].append({**p, "exit": o, "exit_day": today, "ret": p["fill_ret"], "pnl": p["fill_pnl"],
                                "provisional": True})
            s["events"].append({"id": f"{today}-SOLD-{p['sym']}", "day": today, "kind": "SOLD", "sym": p["sym"],
                                "text": f"Paper-sold {p['sym']} at today's open {o:.2f} ({p['exit_due']}): "
                                        f"{100 * p['fill_ret']:+.1f}% after costs, ₹{p['fill_pnl']:+,.0f} "
                                        f"(provisional — confirmed tonight with NSE's official open)"})
    for q in s["pending"]:
        if q.get("filled_today"):
            o = q["filled_today"]
            s["events"].append({"id": f"{today}-BOUGHT-{q['sym']}", "day": today, "kind": "BOUGHT", "sym": q["sym"],
                                "text": f"Paper-bought {q['fill_qty']} × {q['sym']} at today's open {o:.2f}; stop "
                                        f"{o * (1 - STOP):.2f} (−10% on a closing basis), target {o * (1 + TARGET):.2f}, "
                                        f"time exit after {HOLD} trading days (provisional — confirmed tonight)"})
    cl = pd.DataFrame(s["closed"])
    tally = {"trades": len(cl)}
    if len(cl):
        tally.update(win=round(float((cl.ret > 0).mean()), 3), avg=round(float(cl.ret.mean()), 4), pnl=int(cl.pnl.sum()))
    for p in s["positions"]:
        if p["sym"] in live:
            p["ltp"] = live[p["sym"]]
            p["live_ret"] = round(p["ltp"] / p["entry"] - 1, 4)
            p["below_stop_now"] = p["ltp"] <= p["stop"]
    openp = sum(p["qty"] * (p.get("ltp", p.get("last", p["entry"])) - p["entry"]) for p in s["positions"])
    bt = json.loads((DIR / "backtest.json").read_text()) if (DIR / "backtest.json").exists() else None
    M_ = load_model()
    return {"status": status, "rules": {"universe": UNIVERSE, "new_per_week": K_NEW, "slots": SLOTS, "hold_days": HOLD,
                                        "stop": STOP, "target": TARGET, "cost_side": COST, "capital": CAPITAL},
            "model": M_["meta"] if M_ else None, "live_from": s["live_from"], "positions": s["positions"],
            "live": bool(live), "pending": s["pending"], "closed": s["closed"][-50:], "tally": tally, "open_pnl": round(openp),
            "events": s["events"][-60:], "latest": latest, "official": official, "backtest": bt}


def watch_close(now: dt.datetime) -> int:
    """15:20–15:29: warn about open positions trading below their stop (the rule sells only on a CLOSE below it)."""
    with _lock:
        s = _read_state()
        live = live_prices([p["sym"] for p in s["positions"] if not p.get("exit_due")])
        n = 0
        for p in s["positions"]:
            px = live.get(p["sym"])
            eid = f"{now.date()}-WATCH-{p['sym']}"
            if px is not None and px <= p["stop"] and not any(e["id"] == eid for e in s["events"]):
                _event(s, "WATCH", p["sym"], f"{p['sym']} is at {px:.2f}, below its stop {p['stop']:.2f}, near the close. "
                                             f"If it closes below, the rule sells at tomorrow's open.", now.date())
                n += 1
        if n:
            _write_state(s)
        return n


def loop():
    from .sync import now_ist
    last_try = 0.0
    while True:
        try:
            t = now_ist()
            m = t.hour * 60 + t.minute
            if t.weekday() < 5 and 15 * 60 + 20 <= m <= 15 * 60 + 29:
                watch_close(t)
            due = (DIR / "work.parquet").exists() and load_model() is not None
            # NSE publishes the bhavcopy ~18:00–19:00; try every 15 min in the evening, hourly otherwise (catch-up)
            gap = 900 if 18 * 60 <= m <= 23 * 60 + 30 else 3600
            if due and time.time() - last_try > gap:
                last_try = time.time()
                update()
        except Exception as e:
            log.exception("swing loop")
            status["error"] = str(e)[:200]
        time.sleep(60)


def start():
    if config.OFFLINE:
        return
    DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=loop, daemon=True, name="swing").start()


if __name__ == "__main__":                       # docker exec chartlab python -m app.swing train | update | rank
    import sys
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "update"
    if cmd == "train":
        print(train())
    elif cmd == "update":
        print(update())
    elif cmd == "rank":
        print(update(force_rank=True))
