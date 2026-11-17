"""ChartLab's records in one SQLite database (data/chartlab.db): paper-trading accounts / orders / positions /
trades / events, Morning Edge decisions, the swing portfolio and its picks, and the practice journal.

Price history stays in Parquet files (much faster for backtests); analytics.py puts SQL (DuckDB) over both.
WAL mode: the app writes while research scripts or the SQL page read. Every caller opens its own short connection
(thread-safe). On first start the old JSON files are imported and renamed *.migrated (never deleted).
Nightly backup: data/backups/chartlab-<date>.db (SQLite online backup, the last 14 kept).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

log = logging.getLogger("chartlab.db")
PATH = config.DATA_DIR / "chartlab.db"
_init_lock = threading.Lock()
_ready = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_account (account TEXT PRIMARY KEY, settings TEXT, cash REAL, created TEXT);
CREATE TABLE IF NOT EXISTS paper_position (id TEXT PRIMARY KEY, account TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS paper_order (id TEXT PRIMARY KEY, account TEXT, at TEXT, status TEXT, tsym TEXT, side TEXT,
    lots INTEGER, data TEXT);
CREATE TABLE IF NOT EXISTS paper_trade (id TEXT, account TEXT, symbol TEXT, tsym TEXT, type TEXT, source TEXT,
    opened TEXT, closed TEXT, why TEXT, lots INTEGER, entry REAL, exit REAL, gross REAL, charges REAL, net REAL,
    note TEXT, data TEXT, PRIMARY KEY (account, id));
CREATE TABLE IF NOT EXISTS paper_event (id TEXT PRIMARY KEY, account TEXT, at TEXT, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS paper_group (id TEXT PRIMARY KEY, account TEXT, opened TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS edge_signal (day TEXT PRIMARY KEY, pred REAL, signal INTEGER, model_used TEXT, at TEXT,
    data TEXT);
CREATE TABLE IF NOT EXISTS swing_meta (key TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS swing_position (sym TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS swing_trade (n INTEGER PRIMARY KEY AUTOINCREMENT, sym TEXT, entry_day TEXT, exit_day TEXT,
    entry REAL, exit REAL, ret REAL, pnl REAL, why TEXT, data TEXT, UNIQUE (sym, entry_day));
CREATE TABLE IF NOT EXISTS swing_event (id TEXT PRIMARY KEY, day TEXT, kind TEXT, sym TEXT, text TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS swing_pick (day TEXT PRIMARY KEY, official INTEGER, data TEXT);
CREATE TABLE IF NOT EXISTS journal (n INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_trade_closed ON paper_trade (account, closed);
CREATE INDEX IF NOT EXISTS ix_event_acct ON paper_event (account, at);
CREATE INDEX IF NOT EXISTS ix_order_acct ON paper_order (account, status, at);
"""


@contextmanager
def conn():
    """A short-lived connection; commits on success, rolls back on error."""
    init()
    c = sqlite3.connect(PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=10000")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    global _ready
    if _ready:
        return
    with _init_lock:
        if _ready:
            return
        PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(PATH, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        c.commit()
        c.close()
        _ready = True
        try:
            migrate()
        except Exception:
            log.exception("migrating JSON records into SQLite")


def j(x) -> str:
    return json.dumps(x, default=str)


# ───────────────────────────── paper trading ─────────────────────────────
def paper_load(account: str) -> dict | None:
    with conn() as c:
        a = c.execute("SELECT * FROM paper_account WHERE account=?", (account,)).fetchone()
        if a is None:
            return None
        pos = [json.loads(r["data"]) for r in c.execute("SELECT data FROM paper_position WHERE account=? ORDER BY rowid", (account,))]
        orders = [json.loads(r["data"]) for r in c.execute(
            "SELECT data FROM (SELECT data, at FROM paper_order WHERE account=? AND status='open' UNION ALL "
            "SELECT data, at FROM (SELECT data, at FROM paper_order WHERE account=? AND status!='open' ORDER BY at DESC LIMIT 200)) "
            "ORDER BY at", (account, account))]
        trades = [json.loads(r["data"]) for r in c.execute("SELECT data FROM paper_trade WHERE account=? ORDER BY closed", (account,))]
        events = [dict(r) for r in c.execute(
            "SELECT id, at, kind, text FROM (SELECT * FROM paper_event WHERE account=? ORDER BY at DESC, rowid DESC LIMIT 400) "
            "ORDER BY at, rowid", (account,))]
        groups = [json.loads(r["data"]) for r in c.execute("SELECT data FROM paper_group WHERE account=? ORDER BY opened", (account,))]
    return {"groups": groups, "account": account, "created": a["created"], "settings": json.loads(a["settings"]), "cash": a["cash"],
            "positions": pos, "orders": orders, "trades": trades, "events": events}


def paper_save(st: dict):
    acct = st["account"]
    with conn() as c:
        c.execute("INSERT INTO paper_account VALUES (?,?,?,?) ON CONFLICT(account) DO UPDATE SET settings=excluded.settings, "
                  "cash=excluded.cash", (acct, j(st["settings"]), st["cash"], st["created"]))
        c.execute("DELETE FROM paper_position WHERE account=?", (acct,))
        c.executemany("INSERT INTO paper_position VALUES (?,?,?)", [(p["id"], acct, j(p)) for p in st["positions"]])
        c.executemany("INSERT INTO paper_order VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
                      "data=excluded.data", [(o["id"], acct, o["at"], o["status"], o.get("tsym"), o.get("side"), o.get("lots"), j(o))
                                             for o in st["orders"]])
        c.executemany("INSERT OR IGNORE INTO paper_trade VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      [(t["id"], acct, t.get("symbol"), t.get("tsym"), t.get("type"), t.get("source"), t.get("opened"),
                        t.get("closed"), t.get("why"), t.get("lots"), t.get("entry"), t.get("exit"), t.get("gross"),
                        t.get("charges"), t.get("net"), t.get("note"), j(t)) for t in st["trades"]])
        c.executemany("INSERT OR IGNORE INTO paper_event VALUES (?,?,?,?,?)",
                      [(e["id"], acct, e["at"], e["kind"], e["text"]) for e in st["events"]])
        c.executemany("INSERT INTO paper_group VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                      [(g["id"], acct, g["opened"], j(g)) for g in st.get("groups", [])])


def paper_archive(account: str) -> str:
    """Keep an account's history under a new name (e.g. manual@20261017-1530) and free the name for a fresh start."""
    tag = f"{account}@{dt.datetime.now():%Y%m%d-%H%M%S}"
    with conn() as c:
        for t in ("paper_trade", "paper_order", "paper_event", "paper_position", "paper_group"):
            c.execute(f"UPDATE {t} SET account=? WHERE account=?", (tag, account))
        c.execute("UPDATE paper_account SET account=? WHERE account=?", (tag, account))
    return tag


# ───────────────────────────── Morning Edge ─────────────────────────────
def edge_put(out: dict):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO edge_signal VALUES (?,?,?,?,?,?)",
                  (out["day"], out.get("pred"), out.get("signal"), out.get("model_used"), out.get("at"), j(out)))


def edge_get(day: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT data FROM edge_signal WHERE day=?", (day,)).fetchone()
    return json.loads(r["data"]) if r else None


def edge_all() -> list:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT day, pred, signal FROM edge_signal ORDER BY day")]


# ───────────────────────────── swing ─────────────────────────────
def swing_load() -> dict | None:
    with conn() as c:
        meta = {r["key"]: json.loads(r["data"]) for r in c.execute("SELECT key, data FROM swing_meta")}
        if not meta:
            return None
        pos = [json.loads(r["data"]) for r in c.execute("SELECT data FROM swing_position ORDER BY rowid")]
        closed = [json.loads(r["data"]) for r in c.execute("SELECT data FROM swing_trade ORDER BY n")]
        ev = [dict(r) for r in c.execute("SELECT id, day, kind, sym, text, at FROM (SELECT *, rowid AS r FROM swing_event "
                                         "ORDER BY r DESC LIMIT 300) ORDER BY r")]
    return {"live_from": meta.get("live_from"), "pending": meta.get("pending", []), "decided": meta.get("decided", []),
            "positions": pos, "closed": closed, "events": ev}


def swing_save(s: dict):
    with conn() as c:
        c.executemany("INSERT OR REPLACE INTO swing_meta VALUES (?,?)",
                      [("live_from", j(s["live_from"])), ("pending", j(s["pending"])), ("decided", j(s["decided"]))])
        c.execute("DELETE FROM swing_position")
        c.executemany("INSERT INTO swing_position VALUES (?,?)", [(p["sym"], j(p)) for p in s["positions"]])
        c.executemany("INSERT OR IGNORE INTO swing_trade (sym, entry_day, exit_day, entry, exit, ret, pnl, why, data) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      [(t["sym"], t["entry_day"], t.get("exit_day"), t.get("entry"), t.get("exit"), t.get("ret"), t.get("pnl"),
                        t.get("exit_due"), j(t)) for t in s["closed"]])
        c.executemany("INSERT OR IGNORE INTO swing_event VALUES (?,?,?,?,?,?)",
                      [(e["id"], e["day"], e["kind"], e["sym"], e["text"], e["at"]) for e in s["events"]])


def swing_clear():
    with conn() as c:
        for t in ("swing_meta", "swing_position", "swing_trade", "swing_event"):
            c.execute(f"DELETE FROM {t}")


def pick_put(day: str, pk: dict):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO swing_pick VALUES (?,?,?)", (day, int(bool(pk.get("official"))), j(pk)))


def pick_latest(official: bool = False) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT data FROM swing_pick" + (" WHERE official=1" if official else "") + " ORDER BY day DESC LIMIT 1").fetchone()
    return json.loads(r["data"]) if r else None


def pick_exists(day: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM swing_pick WHERE day=?", (day,)).fetchone() is not None


# ───────────────────────────── journal ─────────────────────────────
def journal_all() -> list:
    with conn() as c:
        return [json.loads(r["data"]) for r in c.execute("SELECT data FROM journal ORDER BY n")]


def journal_add(t: dict):
    with conn() as c:
        c.execute("INSERT INTO journal (at, data) VALUES (?,?)", (dt.datetime.now().isoformat(timespec="seconds"), j(t)))


def journal_clear():
    with conn() as c:
        c.execute("DELETE FROM journal")


# ───────────────────────────── one-time import of the old JSON files ─────────────────────────────
def _done(p: Path):
    p.rename(p.with_name(p.name + ".migrated"))


def migrate():
    d = config.DATA_DIR
    for p in sorted((d / "paper").glob("*.json")) if (d / "paper").exists() else []:
        st = json.loads(p.read_text())
        acct = st.get("account") or p.stem
        if "_archive_" in p.stem:                                  # old reset archives keep their history
            st["account"] = acct = p.stem.replace("_archive_", "@")
        if paper_load(acct) is None:
            st.setdefault("created", dt.datetime.now().isoformat(timespec="seconds"))
            paper_save(st)
        _done(p)
        log.info("imported paper account %s", acct)
    sig = d / "edge" / "signals"
    if sig.exists():
        for f in sorted(sig.glob("*.json")):
            edge_put(json.loads(f.read_text()))
        _done(sig)
        log.info("imported Morning Edge decisions")
    sw = d / "swing"
    if (sw / "state.json").exists():
        if swing_load() is None:
            swing_save(json.loads((sw / "state.json").read_text()))
        _done(sw / "state.json")
    if (sw / "picks").exists():
        for f in sorted((sw / "picks").glob("*.json")):
            pick_put(f.stem, json.loads(f.read_text()))
        _done(sw / "picks")
    jp = d / "journal.json"
    if jp.exists():
        with conn() as c:
            if c.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0:
                c.executemany("INSERT INTO journal (at, data) VALUES (?,?)",
                              [(t.get("exit_time") or t.get("time") or "", j(t)) for t in json.loads(jp.read_text())])
        _done(jp)


# ───────────────────────────── backups ─────────────────────────────
def backup(keep: int = 14) -> Path:
    out = config.DATA_DIR / "backups" / f"chartlab-{dt.date.today():%Y%m%d}.db"
    out.parent.mkdir(parents=True, exist_ok=True)
    init()
    src = sqlite3.connect(PATH)
    dst = sqlite3.connect(out)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    for old in sorted(out.parent.glob("chartlab-*.db"))[:-keep]:
        old.unlink()
    return out


def backup_loop():
    while True:
        try:
            p = config.DATA_DIR / "backups" / f"chartlab-{dt.date.today():%Y%m%d}.db"
            if not p.exists():
                log.info("database backup → %s", backup())
        except Exception:
            log.exception("database backup")
        time.sleep(3600)


def start():
    init()
    if not config.OFFLINE:
        threading.Thread(target=backup_loop, daemon=True, name="db-backup").start()
