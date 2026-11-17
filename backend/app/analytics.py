"""SQL over everything ChartLab stores — DuckDB, no server. Read-only.

Views (re-created for every query on a fresh in-memory DuckDB, so nothing persists and nothing can be changed):
  bars            synced price bars: symbol, tf (1m 5m 60m Dm), date, open, high, low, close, volume
                  (recent / synced data; older NSE history lives in the Kaggle CSVs → index_1m('NIFTY 50'))
  index_1m(sym)   table macro: the full Kaggle 1-minute history of an NSE index, e.g. FROM index_1m('NIFTY 50')
  options         every saved index-option minute: underlying, expiry, ts (IST), strike, type, open … close, volume
  nse_fo          NIFTY F&O bhavcopy (daily, futures + options, OI) · participant  NSE participant-wise OI
  stocks_daily    adjusted daily bars of every NSE stock (swing data, last ~420 days) · stocks_history (2010 →)
  mcx_1m          MCX 1-minute history (Angel)
  paper_trades / paper_orders / paper_events / paper_positions / paper_accounts
  edge_signals, swing_trades, swing_positions, swing_events, swing_picks, journal   (from data/chartlab.db)
Safety: only SELECT / WITH / FROM / DESCRIBE / SHOW / SUMMARIZE / EXPLAIN, one statement; file access is limited to
the data folders and the configuration is locked before the user's query runs (no COPY / INSTALL / ATTACH out).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time

import pandas as pd

from . import config, db

MAX_ROWS = 5000
ALLOWED = re.compile(r"^\s*(select|with|from|describe|show|summarize|explain|pivot|unpivot|values|table)\b", re.I)
RECORDS = {
    "paper_trades": "SELECT account, id, symbol, tsym, type, source, opened, closed, why, lots, entry, exit, gross, charges, "
                    "net, note FROM paper_trade",
    "paper_orders": "SELECT account, id, at AS ts, status, tsym, side, lots FROM paper_order",
    "paper_events": "SELECT account, at AS ts, kind, text FROM paper_event",
    "paper_positions": "SELECT account, id, data FROM paper_position",
    "paper_accounts": "SELECT account, cash, created, settings FROM paper_account",
    "edge_signals": "SELECT day, pred, signal, model_used, at AS ts, data FROM edge_signal",
    "swing_trades": "SELECT sym, entry_day, exit_day, entry, exit, ret, pnl, why FROM swing_trade",
    "swing_positions": "SELECT sym, data FROM swing_position",
    "swing_events": "SELECT day, kind, sym, text, at AS ts FROM swing_event",
    "swing_picks": "SELECT day, official, data FROM swing_pick",
    "journal": "SELECT n, at AS ts, data FROM journal",
}
EXAMPLES = [
    ("Paper results by account", "SELECT account, count(*) AS trades, round(avg((net > 0)::INT) * 100) AS win_pct, round(sum(net), 2) AS net, "
                                 "round(sum(charges), 2) AS charges\nFROM paper_trades GROUP BY account ORDER BY account"),
    ("Paper trades by day", "SELECT account, closed::DATE AS day, count(*) AS trades, round(sum(net), 2) AS net\nFROM paper_trades "
                            "GROUP BY ALL ORDER BY day DESC, account"),
    ("Morning Edge live decisions", "SELECT day, round(pred, 3) AS pred, signal, model_used, ts FROM edge_signals ORDER BY day DESC"),
    ("Swing closed trades", "SELECT * FROM swing_trades ORDER BY exit_day DESC"),
    ("NIFTY daily range, last 30 days", "SELECT date::DATE AS day, round(max(high) - min(low), 2) AS range_pts, "
                                        "round(first(open ORDER BY date), 2) AS open, round(last(close ORDER BY date), 2) AS close\nFROM bars WHERE symbol = 'NIFTY 50' AND tf = '1m'\n"
                                        "GROUP BY ALL ORDER BY day DESC LIMIT 30"),
    ("NIFTY option chain at a minute", "SELECT strike, type, close FROM options\nWHERE underlying = 'NIFTY' AND expiry = "
                                       "DATE '2026-10-19' AND ts = TIMESTAMP '2026-10-16 10:00'\nORDER BY strike, type"),
    ("ITM-200 CE price through a day", "SELECT ts, close FROM options WHERE underlying = 'NIFTY' AND expiry = DATE '2026-10-19'\n"
                                       "AND strike = 22900 AND type = 'CE' AND ts::DATE = DATE '2026-10-16' ORDER BY ts"),
    ("Top 10 stocks by 1-month return", "WITH c AS (SELECT sym, date, close FROM stocks_daily WHERE series = 'EQ')\n"
                                        "SELECT sym, round((last(close ORDER BY date) / first(close ORDER BY date) - 1) * 100, 1) AS ret_pct\n"
                                        "FROM c WHERE date >= (SELECT max(date) FROM c) - INTERVAL 30 DAY\n"
                                        "GROUP BY sym HAVING count(*) > 15 ORDER BY ret_pct DESC LIMIT 10"),
]


def _q(p) -> str:
    return str(p).replace("'", "''")


def connect():
    import duckdb
    d = config.DATA_DIR
    c = duckdb.connect()
    c.execute("SET enable_progress_bar=false")
    c.execute("SET TimeZone='Asia/Kolkata'")
    views = {
        "bars": f"""SELECT regexp_extract(filename, '([^/]+)_(1m|5m|60m|Dm)\\.parquet$', 1) AS symbol,
                    replace(regexp_extract(filename, '_(1m|5m|60m|Dm)\\.parquet$', 1), 'Dm', 'D') AS tf,
                    date, open, high, low, close, volume
                    FROM read_parquet('{_q(d / "cache" / "ext")}/*.parquet', filename=true, union_by_name=true)""",
        "options": f"""SELECT regexp_extract(filename, 'options/([A-Z]+)/', 1) AS underlying,
                    CAST(regexp_extract(filename, '(\\d{{4}}-\\d{{2}}-\\d{{2}})\\.parquet$', 1) AS DATE) AS expiry,
                    timezone('Asia/Kolkata', "timestamp"::TIMESTAMPTZ) AS ts, CAST(strike AS DOUBLE) AS strike,
                    option_type AS type, open, high, low, close, volume
                    FROM read_parquet('{_q(config.HF_DIR / "options")}/*/*.parquet', filename=true, union_by_name=true)""",
        "nse_fo": f"SELECT * EXCLUDE (__index_level_0__) FROM read_parquet('{_q(d / 'edge' / 'fo_NIFTY.parquet')}')",
        "participant": f"SELECT * FROM read_parquet('{_q(d / 'edge' / 'participant.parquet')}')",
        "stocks_daily": f"SELECT * FROM read_parquet('{_q(d / 'swing' / 'work.parquet')}')",
        "stocks_history": f"SELECT * FROM read_parquet('{_q(d / 'swing' / 'panel.parquet')}')",
        "mcx_1m": f"""SELECT regexp_extract(filename, 'angel_hist/([^/]+)/', 1) AS symbol, *
                    FROM read_parquet('{_q(d / "angel_hist")}/*/1m_*.parquet', filename=true, union_by_name=true)""",
    }
    exists = {"nse_fo": d / "edge" / "fo_NIFTY.parquet", "participant": d / "edge" / "participant.parquet",
              "stocks_daily": d / "swing" / "work.parquet", "stocks_history": d / "swing" / "panel.parquet"}
    for name, sql in views.items():
        if name in exists and not exists[name].exists():
            continue
        try:
            c.execute(f"CREATE VIEW {name} AS {sql}")
        except Exception:
            pass                                             # e.g. no MCX history downloaded yet
    kag = config.INDEX_DIR if str(config.INDEX_DIR) not in ("", ".") and config.INDEX_DIR.is_dir() else None
    if kag:
        c.execute(f"""CREATE MACRO index_1m(sym) AS TABLE
                      SELECT * FROM read_csv('{_q(kag)}/' || sym || '_minute.csv', auto_detect=true)""")
    db.init()
    s = sqlite3.connect(f"file:{db.PATH}?mode=ro", uri=True)
    try:
        for name, sql in RECORDS.items():
            df = pd.read_sql_query(sql, s)
            for col in df.columns:                           # pandas 3 "str" dtype → object (DuckDB 1.3 reads that)
                if df[col].dtype.name in ("str", "string"):
                    df[col] = df[col].astype(object)
            c.register(f"_{name}", df)
            c.execute(f"CREATE VIEW {name} AS SELECT * FROM _{name}")
    finally:
        s.close()
    dirs = [str(d), str(config.HF_DIR)] + ([str(kag)] if kag else [])
    dirs = sorted({x.rstrip("/") + "/" for x in dirs})       # DuckDB matches folder prefixes ending in "/";
    dirs = [x for x in dirs if not any(x != y and x.startswith(y) for y in dirs)]   # a nested one breaks both
    c.execute("SET allowed_directories=[" + ", ".join(f"'{_q(x)}'" for x in dict.fromkeys(dirs)) + "]")
    c.execute("SET enable_external_access=false")
    c.execute("SET lock_configuration=true")
    return c


def check(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise ValueError("empty query")
    if ";" in s:
        raise ValueError("one statement at a time (no ';' inside)")
    if not ALLOWED.match(s):
        raise ValueError("read-only: start with SELECT, WITH, FROM, DESCRIBE, SHOW, SUMMARIZE or EXPLAIN")
    if re.search(r"\b(copy|attach|detach|install|load|export|import|pragma|set|create|insert|update|delete|drop|alter|call)\b",
                 re.sub(r"'[^']*'", "''", s), re.I):
        raise ValueError("read-only: that statement is not allowed here")
    return s


def query(sql: str, limit: int = MAX_ROWS) -> dict:
    s = check(sql)
    t = time.time()
    c = connect()
    try:
        cur = c.execute(s)
        cols = [x[0] for x in cur.description] if cur.description else []
        rows = cur.fetchmany(limit + 1)
    finally:
        c.close()
    trunc = len(rows) > limit
    rows = rows[:limit]
    def cell(v):
        if isinstance(v, dt.datetime):
            return v.isoformat(sep=" ")
        if isinstance(v, (dt.date, dt.time, dt.timedelta)):
            return str(v)
        if isinstance(v, float) and v != v:
            return None
        return v
    out = [[cell(v) for v in r] for r in rows]
    return {"columns": cols, "rows": out, "truncated": trunc, "ms": round((time.time() - t) * 1000)}


def tables() -> list:
    c = connect()
    try:
        out = []
        for (name,) in c.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal AND view_name NOT LIKE '\\_%' "
                                 "ESCAPE '\\' ORDER BY view_name").fetchall():
            cols = c.execute(f"DESCRIBE {name}").fetchall()
            out.append({"name": name, "columns": [{"name": x[0], "type": x[1]} for x in cols]})
        return out
    finally:
        c.close()
