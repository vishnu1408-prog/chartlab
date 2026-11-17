import json

from app import config, db, paper


def test_paper_account_round_trip_keeps_history_and_reset_archives_it():
    st = paper._new("manual")
    st["trades"].append({"id": "t1", "symbol": "NIFTY 50", "tsym": "X", "type": "CE", "source": "manual",
                         "opened": "2026-10-19T09:46:00", "closed": "2026-10-19T10:30:00", "why": "stop", "lots": 1,
                         "entry": 300.0, "exit": 270.0, "gross": -1950.0, "charges": 60.0, "net": -2010.0, "note": "n", "fills": []})
    st["events"].append({"id": "e1", "at": "2026-10-19T10:30:00", "kind": "CLOSED", "text": "x"})
    st["orders"].append({"id": "o1", "at": "2026-10-19T09:46:00", "status": "filled", "tsym": "X", "side": "BUY", "lots": 1})
    st["cash"] = 147_990.0
    paper.save(st)
    back = paper.load("manual")
    assert back["cash"] == 147_990.0 and back["trades"][0]["net"] == -2010.0 and back["events"][0]["kind"] == "CLOSED"
    paper.save(back)                                                     # saving twice must not duplicate
    assert len(paper.load("manual")["trades"]) == 1
    paper.reset("manual")
    assert paper.load("manual")["trades"] == [] and paper.load("manual")["cash"] == 150_000
    with db.conn() as c:
        rows = c.execute("SELECT account FROM paper_trade WHERE id='t1'").fetchall()
    assert rows and rows[0]["account"].startswith("manual@")


def test_edge_signals_and_journal():
    db.edge_put({"day": "2030-01-02", "pred": -0.2, "signal": -1, "model_used": "full", "at": "x"})
    assert db.edge_get("2030-01-02")["signal"] == -1
    assert any(r["day"] == "2030-01-02" for r in db.edge_all())
    db.journal_clear()
    db.journal_add({"symbol": "NIFTY 50", "pnl_rs": 100})
    assert db.journal_all() == [{"symbol": "NIFTY 50", "pnl_rs": 100}]


def test_old_json_files_are_imported_once_and_renamed(tmp_path, monkeypatch):
    d = config.DATA_DIR
    (d / "paper").mkdir(exist_ok=True)
    old = paper._new("bot")
    old["cash"] = 123.0
    (d / "paper" / "bot.json").write_text(json.dumps(old))
    db.migrate()
    assert paper.load("bot")["cash"] == 123.0
    assert not (d / "paper" / "bot.json").exists() and (d / "paper" / "bot.json.migrated").exists()


def test_backup_writes_a_readable_copy():
    p = db.backup()
    import sqlite3
    c = sqlite3.connect(p)
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='paper_trade'").fetchone()[0] == 1
    c.close()


def test_sql_is_read_only_and_sandboxed():
    from app import analytics
    import pytest
    assert analytics.query("SELECT 1 AS x")["rows"] == [[1]]
    for bad in ("SELECT 1; SELECT 2", "COPY (SELECT 1) TO 'x.csv'", "INSTALL httpfs", "DELETE FROM journal",
                "ATTACH 'x.db'", "SET threads=1"):
        with pytest.raises(ValueError):
            analytics.check(bad)
    with pytest.raises(Exception):
        analytics.query("SELECT * FROM read_csv('/etc/passwd')")
    import pandas as pd
    (config.DATA_DIR / "swing").mkdir(exist_ok=True)
    pd.DataFrame({"date": pd.to_datetime(["2026-10-16"]), "sym": ["ABC"], "series": ["EQ"], "close": [10.0]}).to_parquet(
        config.DATA_DIR / "swing" / "work.parquet", index=False)
    assert analytics.query("SELECT sym, close FROM stocks_daily")["rows"] == [["ABC", 10.0]]   # data files readable
    (config.DATA_DIR / "swing" / "work.parquet").unlink()
    names = {t["name"] for t in analytics.tables()}
    assert {"paper_trades", "edge_signals", "swing_trades", "journal"} <= names
