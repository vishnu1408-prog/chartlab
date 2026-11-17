"""FII / DII money flows:
  cash  — NSE provisional cash-market buy / sell / net for FII/FPI and DII (₹ crore). NSE's API gives only the LATEST day
          (published ~18:00–20:00), so it is recorded every evening into data/flows/cash_nse.csv; 2018–2024 history
          from data/edge/fiidii_cash.csv. 2025 → mid-Oct 2026 has no DII source (gap).
  fpi   — NSDL's official daily FPI equity flows (stock-exchange route, ₹ crore, reported a day later), 2015 →, one
          request per month (data/flows/nsdl_fpi.csv) — fills the FII cash gap.
  positions — NSE participant-wise F&O open interest (FII / DII / Pro / Client), kept by edge.update_nse.
"""
from __future__ import annotations

import calendar
import datetime as dt
import logging
import re
import threading
import time

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger("chartlab.flows")
NSE_API = "https://www.nseindia.com/api/fiidiiTradeReact"
NSDL = "https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx"
MON = {m: i for i, m in enumerate(calendar.month_abbr) if m}
CASH_COLS = ["date", "fii_buy", "fii_sell", "fii_net", "dii_buy", "dii_sell", "dii_net"]
status: dict = {"cash_at": None, "fpi_at": None, "error": None}


def _now() -> str:
    from .sync import now_ist
    return now_ist().isoformat(timespec="seconds")


def _dir():
    d = config.DATA_DIR / "flows"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ───────────────────────────── cash (NSE provisional) ─────────────────────────────
def parse_nse(js: list) -> dict | None:
    """NSE fiidiiTradeReact JSON → one row {date, fii_*, dii_*}. Pure."""
    row = {}
    for r in js or []:
        cat = str(r.get("category", "")).upper()
        k = "fii" if cat.startswith("FII") else "dii" if cat.startswith("DII") else None
        if not k:
            continue
        row["date"] = pd.Timestamp(dt.datetime.strptime(r["date"], "%d-%b-%Y").date())
        for a, b in (("buy", "buyValue"), ("sell", "sellValue"), ("net", "netValue")):
            row[f"{k}_{a}"] = float(str(r[b]).replace(",", ""))
    return row if {"date", "fii_net", "dii_net"} <= set(row) else None


def fetch_nse() -> dict | None:
    import requests
    from .edge import UA
    s = requests.Session()
    s.headers.update({**UA, "Accept": "application/json", "Referer": "https://www.nseindia.com/reports/fii-dii"})
    r = s.get(NSE_API, timeout=20)
    if r.status_code != 200:                               # NSE sometimes wants its cookies first
        s.get("https://www.nseindia.com/", timeout=20)
        r = s.get(NSE_API, timeout=20)
    r.raise_for_status()
    return parse_nse(r.json())


def _merge(path, new: pd.DataFrame):
    old = pd.read_csv(path, parse_dates=["date"]) if path.exists() else new.iloc[:0]
    x = pd.concat([old, new]).drop_duplicates("date", keep="last").sort_values("date")
    tmp = path.with_suffix(".tmp")
    x.to_csv(tmp, index=False)
    tmp.replace(path)
    return x


def update_cash() -> dict | None:
    row = fetch_nse()
    if row:
        _merge(_dir() / "cash_nse.csv", pd.DataFrame([row])[CASH_COLS])
        status["cash_at"] = _now()
    return row


def cash() -> pd.DataFrame:
    parts = []
    seed = config.DATA_DIR / "edge" / "fiidii_cash.csv"
    if seed.exists():
        s = pd.read_csv(seed, parse_dates=["date"])
        s = s[~s[CASH_COLS[1:]].duplicated(keep="first")]        # the seed repeats its last day (stale copy)
        parts.append(s.assign(src="NSE (history)"))
    p = _dir() / "cash_nse.csv"
    if p.exists():
        parts.append(pd.read_csv(p, parse_dates=["date"]).assign(src="NSE (recorded)"))
    if not parts:
        return pd.DataFrame(columns=CASH_COLS + ["src"])
    return pd.concat(parts).drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


# ───────────────────────────── NSDL FPI ─────────────────────────────
def parse_nsdl(html: str) -> list:
    """NSDL archive page → [(date, eq_buy, eq_sell, eq_net, debt_net)]. Pure."""
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    num = r"\(?-?[\d,]+\.\d+\)?"
    val = lambda x: -float(x.strip("()").replace(",", "")) if x.startswith("(") else float(x.replace(",", ""))
    rows = []
    for blk in re.split(r"(?=\b\d{2}-[A-Z][a-z]{2}-\d{4} (?:Equity|Debt))", t):
        m = re.match(r"(\d{2})-([A-Z][a-z]{2})-(\d{4}) Equity Stock Exchange (%s) (%s) (%s)" % (num, num, num), blk)
        if not m:
            continue
        d = dt.date(int(m[3]), MON[m[2]], int(m[1]))
        dm = re.search(r"Debt(?:-General Limit)? Stock Exchange (%s) (%s) (%s)" % (num, num, num), blk)
        rows.append((d, val(m[4]), val(m[5]), val(m[6]), val(dm[3]) if dm else np.nan))
    return rows


def fetch_nsdl_month(y: int, m: int) -> list:
    import requests
    from .edge import UA
    s = requests.Session()
    s.headers.update(UA)
    page = s.get(NSDL, timeout=40).text
    f = dict(re.findall(r'name="(__[A-Z]+)" id="[^"]*" value="([^"]*)"', page))
    last = min(dt.date(y, m, calendar.monthrange(y, m)[1]), dt.date.today())
    ds = last.strftime("%d-%b-%Y")
    f.update({"__EVENTTARGET": "btnSubmit1", "__EVENTARGUMENT": "", "hdnDate": ds, "txtDate": ds})
    return parse_nsdl(s.post(NSDL, data=f, timeout=40).text)


def update_fpi(today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    months = {(today.year, today.month)}
    if today.day <= 5:                                      # last days of the previous month arrive late
        p = today.replace(day=1) - dt.timedelta(days=1)
        months.add((p.year, p.month))
    n = 0
    for y, m in sorted(months):
        rows = fetch_nsdl_month(y, m)
        if rows:
            df = pd.DataFrame(rows, columns=["date", "eq_buy", "eq_sell", "eq_net", "debt_net"])
            df["date"] = pd.to_datetime(df.date)
            _merge(_dir() / "nsdl_fpi.csv", df)
            n += len(rows)
        time.sleep(3)
    status["fpi_at"] = _now()
    return n


def fpi() -> pd.DataFrame:
    p = _dir() / "nsdl_fpi.csv"
    if not p.exists():
        return pd.DataFrame(columns=["date", "eq_buy", "eq_sell", "eq_net", "debt_net"])
    x = pd.read_csv(p, parse_dates=["date"], on_bad_lines="skip")
    return x.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


# ───────────────────────────── F&O positions ─────────────────────────────
def positions(pa: pd.DataFrame) -> pd.DataFrame:
    """participant OI → one row per (date, type): index-futures long %, nets and day changes. Pure."""
    if pa.empty:
        return pd.DataFrame()
    x = pa.copy()
    g = lambda c: pd.to_numeric(x[c], errors="coerce")
    out = pd.DataFrame({"date": pd.to_datetime(x["date"]), "type": x["Client Type"].astype(str).str.strip()})
    out["idx_fut_long"], out["idx_fut_short"] = g("Future Index Long"), g("Future Index Short")
    out["idx_fut_net"] = out.idx_fut_long - out.idx_fut_short
    out["idx_fut_long_pct"] = 100 * out.idx_fut_long / (out.idx_fut_long + out.idx_fut_short)
    out["idx_call_net"] = g("Option Index Call Long") - g("Option Index Call Short")
    out["idx_put_net"] = g("Option Index Put Long") - g("Option Index Put Short")
    out["stk_fut_net"] = g("Future Stock Long") - g("Future Stock Short")
    out = out.dropna(subset=["idx_fut_net"]).sort_values(["type", "date"])
    for c in ("idx_fut_net", "idx_fut_long_pct", "idx_call_net", "idx_put_net", "stk_fut_net"):
        out[f"{c}_chg"] = out.groupby("type")[c].diff()
    return out.sort_values(["date", "type"]).reset_index(drop=True)


def _records(df: pd.DataFrame) -> list:
    df = df.copy()
    df["date"] = df.date.dt.strftime("%Y-%m-%d")
    df = df.replace([np.inf, -np.inf], np.nan).astype(object).where(df.notna(), None)
    return df.to_dict("records")


def summary(days: int = 60) -> dict:
    from . import edge
    c, f = cash(), fpi()
    pos = positions(edge.load_participant())
    since = pd.Timestamp(dt.date.today() - dt.timedelta(days=int(days * 1.5) + 10))
    c_last = c.date.max() if len(c) else None
    return {
        "cash": _records(c[c.date >= since]),
        "fpi": _records(f[f.date >= since]),
        "positions": _records(pos[pos.date >= since]),
        "cash_last": str(c_last.date()) if c_last is not None else None,
        "fpi_last": str(f.date.max().date()) if len(f) else None,
        "pos_last": str(pos.date.max().date()) if len(pos) else None,
        "fpi_mtd": round(float(f[f.date >= pd.Timestamp(dt.date.today().replace(day=1))].eq_net.sum()), 2) if len(f) else None,
        "status": status,
    }


def loop():
    from .sync import now_ist
    last_cash = last_fpi = 0.0
    while True:
        try:
            t = now_ist()
            m = t.hour * 60 + t.minute
            evening = 17 * 60 + 30 <= m <= 23 * 60 + 30 or 7 * 60 <= m <= 9 * 60
            if evening and time.time() - last_cash > 1800:
                last_cash = time.time()
                try:
                    update_cash()
                except Exception as e:
                    status["error"] = f"NSE cash: {e}"[:200]
                    log.warning("flows cash: %s", e)
            if evening and time.time() - last_fpi > 3 * 3600:
                last_fpi = time.time()
                try:
                    update_fpi(t.date())
                except Exception as e:
                    status["error"] = f"NSDL: {e}"[:200]
                    log.warning("flows nsdl: %s", e)
        except Exception:
            log.exception("flows loop")
        time.sleep(60)


def start():
    if config.OFFLINE:
        return
    threading.Thread(target=loop, daemon=True, name="flows").start()
