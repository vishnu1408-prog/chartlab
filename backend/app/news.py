"""Market news: live RSS (Economic Times, Business Standard, Mint markets feeds) + historical headlines from GDELT,
stored in a local SQLite database (data/news.db). Times are IST wall-clock seconds (same convention as the charts).

GDELT notes: free, no key, but at most one request every ~5 s; timestamps are when GDELT saw the article (15-minute
steps, UTC) — usually minutes after publication, sometimes later. Historical days are fetched on demand (replay)
or by a background prefetch job, and cached forever.
"""
import calendar
import datetime as dt
import hashlib
import html
import logging
import re
import sqlite3
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, store

log = logging.getLogger("chartlab.news")
DB = config.DATA_DIR / "news.db"
FEEDS = {
    "ET": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "BS": "https://www.business-standard.com/rss/markets-106.rss",
    "MINT": "https://www.livemint.com/rss/markets",
}
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_QUERY = '(sensex OR nifty OR "stock market" OR "share market" OR RBI OR rupee OR "dalal street" OR "bank nifty") sourcecountry:india sourcelang:english'
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
_db_lock = threading.Lock()
_gdelt_lock = threading.Lock()
_gdelt_last = 0.0
status = {"rss_last": None, "rss_error": "", "gdelt_error": "", "prefetch": None}


def connect():
    con = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS news (id TEXT PRIMARY KEY, t INTEGER, source TEXT, title TEXT, url TEXT,
                   summary TEXT, origin TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS news_t ON news(t)")
    con.execute("CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, n INTEGER, at REAL)")
    return con


def ist_epoch(d: dt.datetime) -> int:
    """aware datetime → IST wall-clock seconds treated as UTC (chart convention)"""
    w = d.astimezone(IST).replace(tzinfo=None)
    return calendar.timegm(w.timetuple())


def _id(url, title):
    return hashlib.sha1((url or title).strip().lower().encode()).hexdigest()[:20]


def clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(s or ""))).strip()


def insert(rows: list) -> int:
    """rows: dicts with t, source, title, url, summary, origin. Returns the number of new headlines."""
    if not rows:
        return 0
    with _db_lock:
        con = connect()
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?,?)",
                        [(_id(r.get("url"), r["title"]), int(r["t"]), r.get("source", ""), r["title"][:400], r.get("url", ""),
                          (r.get("summary") or "")[:600], r.get("origin", "")) for r in rows if r.get("title")])
        con.commit()
        n = con.total_changes - before
        con.close()
    return n


def query(end=None, start=None, q=None, limit=150) -> list:
    sql, args = "SELECT t, source, title, url, summary, origin FROM news WHERE 1=1", []
    if end is not None:
        sql += " AND t <= ?"; args.append(int(end))
    if start is not None:
        sql += " AND t >= ?"; args.append(int(start))
    if q:
        for word in q.split():
            sql += " AND (title LIKE ? OR summary LIKE ?)"; args += [f"%{word}%", f"%{word}%"]
    sql += " ORDER BY t DESC LIMIT ?"; args.append(int(limit))
    with _db_lock:
        con = connect()
        rows = con.execute(sql, args).fetchall()
        con.close()
    return [dict(zip(("time", "source", "title", "url", "summary", "origin"), r)) for r in rows]


def counts() -> dict:
    with _db_lock:
        con = connect()
        n = con.execute("SELECT COUNT(*), MIN(t), MAX(t) FROM news").fetchone()
        d = con.execute("SELECT COUNT(*) FROM days").fetchone()[0]
        con.close()
    return {"headlines": n[0], "first": n[1], "last": n[2], "days_cached": d}


# ───────────────────────────── live RSS ─────────────────────────────
def parse_rss(text: str, source: str) -> list:
    out = []
    for block in re.findall(r"<item\b.*?</item>", text, flags=re.S):
        it = {}
        for tag in ("title", "link", "pubDate", "description"):
            m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", block, flags=re.S)
            v = m.group(1) if m else ""
            it[tag] = re.sub(r"^\s*<!\[CDATA\[(.*)\]\]>\s*$", r"\1", v, flags=re.S).strip()
        try:
            t = ist_epoch(parsedate_to_datetime(it["pubDate"]))
        except Exception:
            continue
        out.append({"t": t, "source": source, "title": clean(it["title"]), "url": clean(it["link"]),
                    "summary": clean(it["description"]), "origin": "rss"})
    return out


def poll_rss() -> int:
    import requests
    new = 0
    for src, url in FEEDS.items():
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 ChartLab"})
            r.raise_for_status()
            new += insert(parse_rss(r.text, src))
        except Exception as e:
            status["rss_error"] = f"{src}: {e}"
    status["rss_last"] = time.time()
    return new


def rss_loop():
    while True:
        try:
            poll_rss()
        except Exception as e:                     # never let the poller die
            status["rss_error"] = str(e)
        time.sleep(120)


def import_csv(path: Path) -> int:
    """headlines saved by the research project's logger (indicators/data/news/news_live.csv)"""
    try:
        d = pd.read_csv(path)
    except Exception:
        return 0
    rows = []
    for _, x in d.iterrows():
        try:
            t = calendar.timegm(pd.Timestamp(x["published_ist"]).timetuple())
        except Exception:
            continue
        rows.append({"t": t, "source": x.get("source", ""), "title": str(x.get("title", "")), "url": str(x.get("link", "")),
                     "summary": str(x.get("summary", "") or ""), "origin": "import"})
    return insert(rows)


# ───────────────────────────── GDELT history ─────────────────────────────
def _gdelt_get(params: dict) -> list:
    """rate-limited GDELT call (≥ 6 s apart; backs off on HTTP 429)"""
    global _gdelt_last
    import requests
    for attempt in range(4):
        with _gdelt_lock:
            wait = 6.0 - (time.time() - _gdelt_last)
            if wait > 0:
                time.sleep(wait)
            _gdelt_last = time.time()
            try:
                r = requests.get(GDELT_URL, params=params, timeout=60)
            except Exception as e:
                status["gdelt_error"] = str(e)
                r = None
        if r is not None and r.status_code == 200:
            try:
                return r.json().get("articles", [])
            except ValueError:
                return []                                          # empty day → GDELT returns a non-JSON body
        status["gdelt_error"] = f"HTTP {getattr(r, 'status_code', '?')}"
        time.sleep(20 * (attempt + 1))
    raise RuntimeError(status["gdelt_error"] or "GDELT unavailable")


def fetch_day(day: dt.date) -> int:
    """Download one day's Indian market headlines from GDELT (cached: a day is fetched only once)."""
    key = day.isoformat()
    with _db_lock:
        con = connect()
        hit = con.execute("SELECT n FROM days WHERE day=?", (key,)).fetchone()
        con.close()
    if hit is not None or config.OFFLINE:
        return hit[0] if hit else 0
    s = (dt.datetime.combine(day, dt.time()) - dt.timedelta(hours=5, minutes=30))          # IST day in UTC
    arts = _gdelt_get(dict(query=GDELT_QUERY, mode="ArtList", format="json", maxrecords=250, sort="DateAsc",
                           startdatetime=s.strftime("%Y%m%d%H%M%S"), enddatetime=(s + dt.timedelta(days=1, seconds=-1)).strftime("%Y%m%d%H%M%S")))
    rows = []
    for a in arts:
        try:
            t = ist_epoch(dt.datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc))
        except Exception:
            continue
        rows.append({"t": t, "source": a.get("domain", ""), "title": clean(a.get("title", "")).replace(" , ", ", ").replace(" .", "."),
                     "url": a.get("url", ""), "origin": "gdelt"})
    n = insert(rows)
    with _db_lock:
        con = connect()
        con.execute("INSERT OR REPLACE INTO days VALUES (?,?,?)", (key, len(rows), time.time()))
        con.commit()
        con.close()
    return n


_queue: list = []
_qlock = threading.Lock()
_worker = None


def ensure_days(start: dt.date, end: dt.date, background=True, priority=False):
    """Queue missing weekdays [start, end] for download (oldest first). priority=True puts them at the front
    (the day being replayed should arrive before the rest of the range)."""
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() < 5 and d <= dt.date.today()]
    with _db_lock:
        con = connect()
        have = {r[0] for r in con.execute("SELECT day FROM days")}
        con.close()
    todo = [d for d in days if d.isoformat() not in have]
    with _qlock:
        if priority:
            for d in todo:
                if d in _queue:
                    _queue.remove(d)
            _queue[0:0] = todo
        else:
            for d in todo:
                if d not in _queue:
                    _queue.append(d)
    if background:
        _start_worker()
    return len(todo)


def _start_worker():
    global _worker
    if config.OFFLINE or (_worker and _worker.is_alive()):
        return

    def run():
        while True:
            with _qlock:
                if not _queue:
                    status["prefetch"] = None
                    return
                d = _queue.pop(0)
                status["prefetch"] = {"day": d.isoformat(), "left": len(_queue)}
            try:
                fetch_day(d)
                status["gdelt_error"] = ""
            except Exception as e:
                status["gdelt_error"] = f"{d}: {e}"
    _worker = threading.Thread(target=run, daemon=True)
    _worker.start()


def queue_info():
    with _qlock:
        return {"queued": len(_queue), "current": status["prefetch"]}


# ───────────────────────────── news impact study ─────────────────────────────
def impact(q: str, symbol: str = "NIFTY 50", horizons=(15, 30, 60), limit=400) -> dict:
    """How did `symbol` move after headlines matching `q`? Only headlines during market hours (09:15–15:00) with price
    data count; headlines in the same 15 minutes are counted once. Baseline = the average absolute move over the same
    horizon from every 15-minute mark of the same days (so you can see whether news moves are bigger than normal)."""
    hs = query(q=q, limit=5000)
    m = store.minute_bars(symbol)
    if m.empty or not hs:
        return {"q": q, "symbol": symbol, "events": [], "summary": {}, "n_headlines": len(hs)}
    close = m.close
    idx = m.index
    events, seen = [], set()
    for h in sorted(hs, key=lambda x: x["time"]):
        ts = store.from_epoch(h["time"])
        mod = ts.hour * 60 + ts.minute
        if not (config.SESSION_START <= mod <= 15 * 60):
            continue
        b = (ts.normalize(), mod // 15)
        if b in seen:
            continue
        i = idx.searchsorted(ts, side="right") - 1
        if i < 0 or idx[i].normalize() != ts.normalize() or (ts - idx[i]) > pd.Timedelta(minutes=5):
            continue
        seen.add(b)
        p0 = float(close.iloc[i])
        ev = {"time": h["time"], "title": h["title"], "source": h["source"], "price": p0}
        day_end = ts.normalize() + pd.Timedelta(minutes=config.SESSION_END - 1)
        for hz in horizons:
            j = idx.searchsorted(ts + pd.Timedelta(minutes=hz), side="right") - 1
            ok = j > i and idx[j] <= day_end and idx[j].normalize() == ts.normalize()
            ev[f"ret_{hz}"] = round((float(close.iloc[j]) / p0 - 1) * 100, 3) if ok else None
        events.append(ev)
        if len(events) >= limit:
            break
    summary = {}
    days = sorted({store.from_epoch(e["time"]).normalize() for e in events})
    base_m = m[m.index.normalize().isin(days)]
    for hz in horizons:
        r = np.array([e[f"ret_{hz}"] for e in events if e[f"ret_{hz}"] is not None], dtype=float)
        if not len(r):
            continue
        bc = base_m.close
        marks = bc[(bc.index.minute % 15 == 0)]
        fut = bc.reindex(marks.index + pd.Timedelta(minutes=hz))
        same = fut.index.normalize() == marks.index.normalize()
        base = np.abs((fut.to_numpy() / marks.to_numpy() - 1) * 100)[same]
        base = base[np.isfinite(base)]
        summary[str(hz)] = {"n": int(len(r)), "avg": round(float(r.mean()), 3), "median": round(float(np.median(r)), 3),
                            "up_pct": round(float((r > 0).mean() * 100), 1), "avg_abs": round(float(np.abs(r).mean()), 3),
                            "normal_abs": round(float(base.mean()), 3) if len(base) else None}
    return {"q": q, "symbol": symbol, "events": events[::-1], "summary": summary, "n_headlines": len(hs)}
