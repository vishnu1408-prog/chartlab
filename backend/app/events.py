"""Economic-calendar events for chart markers (IST wall-clock times).

Sources (all public, no login):
  US & global   ForexFactory weekly calendar JSON (nfs.faireconomy.media, this + next week; fetched daily and kept in
                SQLite so history builds up from now) — CPI, jobs report (NFP), PMI, GDP … with impact levels.
  FOMC          federalreserve.gov meeting calendar (2020 →): decision = 2nd day 14:00 ET.
  US NFP        first Friday 08:30 ET before ForexFactory history exists (approximate).
  RBI policy    built-in announcement dates 2022–2025 (10:00 IST); later dates → add them as custom events.
  India CPI     ~12th of each month 16:00 IST (approximate).
  Budget        Union Budget days (11:00 IST).
  Expiries      real expiry dates from the option files / Angel contract lists (NIFTY, BANKNIFTY, SENSEX, MCX).
  EIA (MCX)     US crude inventories Wed 10:30 ET, natural-gas storage Thu 10:30 ET (approximate, holidays ignored).
  Custom        your own events (table event_custom).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import time

from . import config, db

log = logging.getLogger("chartlab.events")
_lock = threading.Lock()
_fetched: dict = {}

RBI = ["2022-02-10", "2022-04-08", "2022-05-04", "2022-06-08", "2022-08-05", "2022-09-30", "2022-12-07",
       "2023-02-08", "2023-04-06", "2023-06-08", "2023-08-10", "2023-10-06", "2023-12-08",
       "2024-02-08", "2024-04-05", "2024-06-07", "2024-08-08", "2024-10-09", "2024-12-06",
       "2025-02-07", "2025-04-09", "2025-06-06", "2025-08-06", "2025-10-01", "2025-12-05"]
BUDGET = {"2015-02-28": "Union Budget", "2016-02-29": "Union Budget", "2017-02-01": "Union Budget",
          "2018-02-01": "Union Budget", "2019-02-01": "Interim Budget", "2019-07-05": "Union Budget",
          "2020-02-01": "Union Budget", "2021-02-01": "Union Budget", "2022-02-01": "Union Budget",
          "2023-02-01": "Union Budget", "2024-02-01": "Interim Budget", "2024-07-23": "Union Budget",
          "2025-02-01": "Union Budget", "2026-02-01": "Union Budget"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_cal (id TEXT PRIMARY KEY, ts TEXT, title TEXT, country TEXT, category TEXT,
    impact TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS event_custom (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, title TEXT, country TEXT,
    impact TEXT);
CREATE INDEX IF NOT EXISTS ix_event_ts ON event_cal (ts);
"""


def _init():
    with db.conn() as c:
        c.executescript(SCHEMA)


def et_to_ist(d: dt.date, h: int, m: int) -> dt.datetime:
    """US Eastern wall time → IST (US DST: 2nd Sunday of March → 1st Sunday of November)."""
    mar = dt.date(d.year, 3, 8)
    dst_start = mar + dt.timedelta(days=(6 - mar.weekday()) % 7)
    nov = dt.date(d.year, 11, 1)
    dst_end = nov + dt.timedelta(days=(6 - nov.weekday()) % 7)
    off = 4 if dst_start <= d < dst_end else 5
    return dt.datetime.combine(d, dt.time(h, m)) + dt.timedelta(hours=off, minutes=330)


# ───────────────────────────── fetchers ─────────────────────────────
def fetch_ff() -> int:
    """ForexFactory this + next week → event_cal (only High / Medium impact)."""
    import requests
    n = 0
    rows = []
    for wk in ("thisweek", "nextweek"):
        try:
            r = requests.get(f"https://nfs.faireconomy.media/ff_calendar_{wk}.json", timeout=20,
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                continue
            for x in r.json():
                if x.get("impact") not in ("High", "Medium"):
                    continue
                t = dt.datetime.fromisoformat(x["date"])
                ist = (t.astimezone(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(minutes=330))
                rows.append((f"ff:{x['country']}:{x['title']}:{ist:%Y%m%d%H%M}", ist.isoformat(timespec="minutes"),
                             x["title"], x["country"], "macro", x["impact"], "ForexFactory"))
        except Exception as e:
            log.warning("forexfactory %s: %s", wk, e)
    if rows:
        _init()
        with db.conn() as c:
            c.executemany("INSERT OR REPLACE INTO event_cal VALUES (?,?,?,?,?,?,?)", rows)
        n = len(rows)
    return n


def fetch_fomc() -> int:
    """FOMC decision days from the Fed's calendar page (2nd meeting day, 14:00 ET)."""
    import requests
    MON = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                       "September", "October", "November", "December"], 1)}
    r = requests.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", timeout=30,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    html = r.text
    rows = []
    for ym in re.finditer(r"(\d{4}) FOMC Meetings(.*?)(?=\d{4} FOMC Meetings|$)", html, re.S):
        year, block = int(ym.group(1)), ym.group(2)
        months = re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([A-Za-z/]+)</strong>', block)
        dates = re.findall(r'fomc-meeting__date[^>]*>([^<]+)<', block)
        for mo, dd in zip(months, dates):
            if "notation" in dd or "cancel" in dd.lower():
                continue
            m2 = mo.split("/")[-1]
            nums = re.findall(r"\d+", dd)
            if not nums or m2 not in MON:
                continue
            day = int(nums[-1])
            month = MON[m2]
            try:
                d = dt.date(year, month, day)
            except ValueError:
                continue
            ist = et_to_ist(d, 14, 0)
            rows.append((f"fomc:{d}", ist.isoformat(timespec="minutes"), "FOMC rate decision", "USD", "fomc",
                         "High", "federalreserve.gov"))
    if rows:
        _init()
        with db.conn() as c:
            c.executemany("INSERT OR REPLACE INTO event_cal VALUES (?,?,?,?,?,?,?)", rows)
    return len(rows)


def refresh(force: bool = False):
    """ForexFactory once a day, the Fed calendar once a week (both quick)."""
    if config.OFFLINE:
        return
    with _lock:
        now = time.time()
        if force or now - _fetched.get("ff", 0) > 86400:
            try:
                fetch_ff()
                _fetched["ff"] = now
            except Exception as e:
                log.warning("events ff: %s", e)
        if force or now - _fetched.get("fomc", 0) > 7 * 86400:
            try:
                fetch_fomc()
                _fetched["fomc"] = now
            except Exception as e:
                log.warning("events fomc: %s", e)


# ───────────────────────────── rule-based + stored ─────────────────────────────
def _expiries(start: dt.date, end: dt.date, markets: set) -> list:
    from . import options
    out = []
    for hf, label, mk in (("NIFTY", "NIFTY expiry", "nse"), ("BANKNIFTY", "BANKNIFTY expiry", "nse"),
                          ("SENSEX", "SENSEX expiry", "nse"), ("CRUDEOIL", "Crude option expiry", "mcx"),
                          ("NATURALGAS", "Natural gas option expiry", "mcx")):
        if mk not in markets:
            continue
        try:
            actual = set(options.dataset_expiries(hf))
        except Exception:
            actual = set()
        try:                                              # live contract lists (future expiries, holiday-shifted)
            from . import chain
            sym = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "SENSEX": "SENSEX", "CRUDEOIL": "MCX CRUDEOIL",
                   "NATURALGAS": "MCX NATURALGAS"}[hf]
            if not config.OFFLINE and chain.supported(sym):
                actual |= {r[0] for r in chain.contracts(sym)[2]}
        except Exception:
            pass
        try:
            rule = options._rule_expiries(hf, start, end)
        except Exception:
            rule = []
        # real dates win; a rule date fills a week that has no real expiry nearby (e.g. expired weeklies not on file)
        days = {d for d in actual if start <= d <= end}
        approx = {r for r in rule if start <= r <= end and not any(abs((r - a).days) <= 3 for a in actual)}
        close = (15, 30) if mk == "nse" else (23, 30)
        for d in sorted(days | approx):
            out.append({"ts": dt.datetime.combine(d, dt.time(*close)), "title": label + (" (approx.)" if d in approx else ""),
                        "country": "IN" if mk == "nse" else "MCX", "category": "expiry", "impact": "Medium",
                        "approx": d in approx})
    return out


def generated(start: dt.date, end: dt.date, markets: set) -> list:
    ev = []
    d = start
    while d <= end:
        if d.isoformat() in BUDGET and "in" in markets:
            ev.append({"ts": dt.datetime.combine(d, dt.time(11, 0)), "title": BUDGET[d.isoformat()], "country": "IN",
                       "category": "budget", "impact": "High", "approx": False})
        if d.isoformat() in RBI and "in" in markets:
            ev.append({"ts": dt.datetime.combine(d, dt.time(10, 0)), "title": "RBI policy decision", "country": "IN",
                       "category": "rbi", "impact": "High", "approx": False})
        if d.day == 12 and "in" in markets:
            ev.append({"ts": dt.datetime.combine(d, dt.time(16, 0)), "title": "India CPI (approx. date)", "country": "IN",
                       "category": "cpi", "impact": "Medium", "approx": True})
        if "us" in markets and d.weekday() == 4 and d.day <= 7:
            ev.append({"ts": et_to_ist(d, 8, 30), "title": "US jobs report (NFP, approx.)", "country": "USD",
                       "category": "nfp", "impact": "High", "approx": True})
        if "eia" in markets and d.weekday() == 2:
            ev.append({"ts": et_to_ist(d, 10, 30), "title": "EIA crude inventories", "country": "USD", "category": "eia",
                       "impact": "Medium", "approx": True})
        if "eia" in markets and d.weekday() == 3:
            ev.append({"ts": et_to_ist(d, 10, 30), "title": "EIA natural-gas storage", "country": "USD", "category": "eia",
                       "impact": "Medium", "approx": True})
        d += dt.timedelta(days=1)
    return ev


def stored(start: dt.datetime, end: dt.datetime) -> list:
    _init()
    with db.conn() as c:
        cal = [dict(r) for r in c.execute("SELECT ts, title, country, category, impact, source FROM event_cal "
                                          "WHERE ts >= ? AND ts <= ?", (start.isoformat(), end.isoformat()))]
        cus = [dict(r) for r in c.execute("SELECT id, ts, title, country, impact FROM event_custom WHERE ts >= ? AND ts <= ?",
                                          (start.isoformat(), end.isoformat()))]
    out = [{**r, "ts": dt.datetime.fromisoformat(r["ts"]), "approx": False} for r in cal]
    out += [{**r, "ts": dt.datetime.fromisoformat(r["ts"]), "category": "custom", "approx": False} for r in cus]
    return out


def events(start: dt.datetime, end: dt.datetime, markets: set, min_impact: str = "Medium") -> list:
    """All events in [start, end] for the chosen markets: in (India), us (US & global macro), nse (expiries),
    mcx (MCX expiries), eia (US energy inventory reports)."""
    ev = stored(start, end)
    have_nfp = any(e["category"] == "macro" and "Non-Farm" in e["title"] for e in ev)
    gen = generated(start.date(), end.date(), markets)
    if have_nfp:
        gen = [g for g in gen if g["category"] != "nfp"]
    ev = [e for e in ev if (e["country"] in ("IN",) and "in" in markets) or
          (e["category"] in ("macro", "fomc") and "us" in markets and e["country"] in ("USD", "All", "CNY")) or
          e["category"] == "custom"] + gen + _expiries(start.date(), end.date(), markets)
    rank = {"Low": 0, "Medium": 1, "High": 2}
    ev = [e for e in ev if rank.get(e.get("impact"), 1) >= rank[min_impact] or e["category"] in ("expiry", "custom")]
    seen, out = set(), []
    for e in sorted(ev, key=lambda e: e["ts"]):
        k = (e["ts"], e["title"])
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def add_custom(ts: str, title: str, country: str = "IN", impact: str = "High") -> int:
    _init()
    t = dt.datetime.fromisoformat(ts)
    with db.conn() as c:
        cur = c.execute("INSERT INTO event_custom (ts, title, country, impact) VALUES (?,?,?,?)",
                        (t.isoformat(timespec="minutes"), title[:80], country[:8], impact))
        return cur.lastrowid


def delete_custom(i: int):
    _init()
    with db.conn() as c:
        c.execute("DELETE FROM event_custom WHERE id=?", (i,))


def custom() -> list:
    _init()
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM event_custom ORDER BY ts")]


def loop():
    while True:
        try:
            refresh()
        except Exception:
            log.exception("events loop")
        time.sleep(3 * 3600)


def start():
    if config.OFFLINE:
        return
    threading.Thread(target=loop, daemon=True, name="events").start()
