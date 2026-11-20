import numpy as np
import pandas as pd

from app import swing


def test_ca_factor_bonus_split_combined_demerger_dividend():
    assert swing.ca_factor("BONUS 1:1", 500) == 0.5
    assert abs(swing.ca_factor("BONUS 3:2", 500) - 0.4) < 1e-9
    assert abs(swing.ca_factor("FV SPLIT RS 10 TO RS 2", 500) - 0.2) < 1e-9
    assert abs(swing.ca_factor("BON1:1/FV SPLT RS10TO RE1", 500) - 0.05) < 1e-9
    assert swing.ca_factor("DEMERGER", 500) == "OPEN"
    assert abs(swing.ca_factor("INTDIV - RS 8 PER SH", 500) - 0.984) < 1e-9
    assert swing.ca_factor("ANNUAL GENERAL MEETING", 500) is None
    assert swing.ca_factor("DIVIDEND RE 1 PER SHARE", 500) is None          # < 0.5 % of price: ignored


def _day(d, rows):
    return pd.DataFrame([dict(date=pd.Timestamp(d), sym=s, series="EQ", open=o, high=max(o, c), low=min(o, c), close=c,
                              prev=p, value=1e8) for s, o, c, p in rows])


def test_add_day_adjusts_history_once_even_if_nse_repeats_the_action():
    work = _day("2024-10-25", [("RELIANCE", 2650, 2655.7, 2640), ("TCS", 4000, 4010, 3990)]).drop(columns="prev")
    new = _day("2024-10-28", [("RELIANCE", 1330, 1334.35, 2655.7), ("TCS", 4012, 4020, 4010)])
    ca = pd.DataFrame({"sym": ["RELIANCE", "RELIANCE"], "series": ["EQ", "EQ"], "ex_dt": [pd.Timestamp("2024-10-28")] * 2,
                       "purpose": ["BONUS 1:1", "BONUS 1:1 "]})
    out, fac = swing.add_day(work, new, ca)
    assert fac == {"RELIANCE": 0.5}
    r = out[out.sym == "RELIANCE"].set_index("date").close
    assert abs(r.iloc[0] - 2655.7 / 2) < 1e-6 and abs(r.iloc[1] - 1334.35) < 1e-6
    t = out[out.sym == "TCS"].set_index("date").close
    assert t.iloc[0] == 4010                                                   # untouched


def test_unrecorded_split_detected_by_standard_ratio_but_not_a_crash():
    new = _day("2025-01-10", [("SPLITCO", 530, 532, 2810), ("CRASHCO", 300, 280, 640)])
    fac = swing.day_factors(new, pd.DataFrame(columns=["sym", "series", "ex_dt", "purpose"]).astype({"ex_dt": "datetime64[ns]"}))
    assert abs(fac["SPLITCO"] - 0.2) < 1e-9                                   # 532 / 2810 ≈ 1/5
    assert "CRASHCO" not in fac                                               # 0.4375: no standard ratio within 6 %


def _wide(days, prices):
    idx = pd.DatetimeIndex(days)
    O = pd.DataFrame({"AAA": [p[0] for p in prices]}, index=idx, dtype="float32")
    C = pd.DataFrame({"AAA": [p[1] for p in prices]}, index=idx, dtype="float32")
    return {"open": O, "close": C}


def test_paper_portfolio_buys_at_open_stops_on_close_and_sells_next_open():
    days = pd.bdate_range("2026-01-05", periods=4)
    W = _wide(days, [(100, 101), (100, 89.9), (88, 90), (91, 92)])
    s = {"live_from": None, "positions": [], "pending": [{"sym": "AAA", "score": 0.1, "reason": "x", "close": 100}],
         "closed": [], "events": [], "decided": []}
    swing.step_portfolio(s, W, days[0], {})
    p = s["positions"][0]
    assert p["entry"] == 100 and p["stop"] == 90 and p["qty"] == int(swing.CAPITAL / swing.SLOTS // 100)
    assert not p.get("exit_due")
    swing.step_portfolio(s, W, days[1], {})                                  # closes 89.9 ≤ 90 → sell next open
    assert s["positions"][0]["exit_due"].startswith("stop")
    assert any(e["kind"] == "SELL" for e in s["events"])
    swing.step_portfolio(s, W, days[2], {})
    assert not s["positions"] and s["closed"][0]["exit"] == 88
    assert abs(s["closed"][0]["ret"] - (88 * (1 - swing.COST) / (100 * (1 + swing.COST)) - 1)) < 1e-4


def test_paper_portfolio_time_exit_and_corporate_action_rescales_entry():
    days = pd.bdate_range("2026-02-02", periods=swing.HOLD + 2)
    W = _wide(days, [(100, 100)] * 5 + [(50, 50)] * (len(days) - 5))
    s = {"live_from": None, "positions": [], "pending": [{"sym": "AAA", "score": 0.1, "reason": "x", "close": 100}],
         "closed": [], "events": [], "decided": []}
    for i, d in enumerate(days[:swing.HOLD]):
        swing.step_portfolio(s, W, d, {"AAA": 0.5} if i == 5 else {})
    p = s["positions"][0]
    assert p["entry"] == 50 and p["stop"] == 45                              # 1:1 bonus: no fake −50 % stop
    assert p["exit_due"].startswith(f"{swing.HOLD} days")
    swing.step_portfolio(s, W, days[swing.HOLD], {})
    assert s["closed"] and abs(s["closed"][0]["ret"]) < 0.01


def test_explain_gives_plain_reasons_from_model():
    rng = np.random.default_rng(0)
    feats = ["p_" + k for k in swing.LABEL] + ["m_r20"]
    X = pd.DataFrame(rng.random((300, len(feats))), columns=feats)
    for k in swing.LABEL:
        X[k] = X["p_" + k]
    X["sym"] = [f"S{i}" for i in range(300)]

    class M:                                                                  # score = 6-month percentile
        def predict(self, Z):
            return np.asarray(Z["p_r120"]) - 0.5
    ex = swing.explain({"model": M(), "feats": feats}, X, [int(X.p_r120.idxmax())])
    (sym, e), = ex.items()
    assert e["for"][0]["what"] == "6-month return" and e["for"][0]["rank"].startswith("top")
    assert "6-month return" in swing.reason_text(e)


def test_chart_api_returns_daily_candles_moving_averages_and_levels():
    from fastapi.testclient import TestClient
    from app.main import app
    swing.DIR.mkdir(parents=True, exist_ok=True)
    days = pd.bdate_range("2025-01-01", periods=260)
    px = 100 + np.arange(260) * 0.5
    pd.DataFrame({"date": days, "sym": "ABC", "series": "EQ", "open": px, "high": px + 1, "low": px - 1, "close": px,
                  "value": 1e8}).to_parquet(swing.DIR / "work.parquet", index=False)
    swing._write_state({"live_from": None, "positions": [{"sym": "ABC", "entry": 200.0, "stop": 180.0, "target": 250.0,
                                                          "qty": 90, "days": 3, "entry_day": "2025-12-01"}],
                        "pending": [], "closed": [], "events": [], "decided": []})
    c = TestClient(app)
    d = c.get("/api/swing/chart", params={"sym": "ABC"}).json()
    assert len(d["daily"]) == 260 and d["daily"][-1]["close"] == px[-1]
    assert len(d["ma50"]) == 211 and len(d["ma200"]) == 61
    assert d["position"]["stop"] == 180.0 and d["live"] is False and d["intraday"] == []
    assert c.get("/api/swing/chart", params={"sym": "NOPE"}).status_code == 404
    assert c.get("/api/swing/chart", params={"sym": "../etc"}).status_code == 422
    __import__("app.db").db.swing_clear()
    (swing.DIR / "work.parquet").unlink()


def test_kickstart_needs_live_prices_and_never_restarts_a_running_portfolio(monkeypatch):
    swing.DIR.mkdir(parents=True, exist_ok=True)
    pk = {"day": "2026-10-14", "top": [{"rank": i + 1, "sym": s, "score": 0.1, "reason": "r"} for i, s in enumerate(["AAA", "BBB", "CCC"])]}
    __import__("app.db").db.pick_put("2026-10-14", pk)
    swing._write_state({"live_from": None, "positions": [], "pending": [], "closed": [], "events": [], "decided": []})
    assert swing.kickstart()["started"] is False                               # offline: no live prices
    monkeypatch.setattr(swing, "live_prices", lambda syms: {s: 100.0 for s in syms})
    r = swing.kickstart()
    assert r == {"started": True, "bought": ["AAA", "BBB"]}
    s = swing._read_state()
    assert [p["sym"] for p in s["positions"]] == ["AAA", "BBB"] and s["positions"][0]["stop"] == 90.0
    assert s["positions"][0]["days"] == 0 and s["events"][-1]["kind"] == "BOUGHT"
    assert swing.kickstart()["started"] is False                               # already running
    __import__("app.db").db.swing_clear()
    with __import__("app.db").db.conn() as c:
        c.execute("DELETE FROM swing_pick")


def test_watch_close_warns_once_when_below_stop(monkeypatch):
    import datetime as dt
    swing.DIR.mkdir(parents=True, exist_ok=True)
    swing._write_state({"live_from": None, "pending": [], "closed": [], "events": [], "decided": [],
                        "positions": [{"sym": "AAA", "entry": 100.0, "stop": 90.0, "target": 125.0, "qty": 1, "days": 3},
                                      {"sym": "BBB", "entry": 100.0, "stop": 90.0, "target": 125.0, "qty": 1, "days": 3}]})
    monkeypatch.setattr(swing, "live_prices", lambda syms: {"AAA": 89.0, "BBB": 95.0})
    now = dt.datetime(2026, 10, 16, 15, 21)
    assert swing.watch_close(now) == 1 and swing.watch_close(now) == 0
    ev = swing._read_state()["events"]
    assert [e["sym"] for e in ev] == ["AAA"] and ev[0]["kind"] == "WATCH"
    __import__("app.db").db.swing_clear()


def test_trailing_stop_locks_profit():
    days = pd.bdate_range("2026-03-02", periods=12)
    closes = [100, 100, 105, 112, 120, 118, 109, 107, 106, 105, 104, 103]       # +20 % then a fall
    W = _wide(days, [(c, c) for c in closes])
    s = {"live_from": None, "positions": [], "pending": [{"sym": "AAA", "score": 0.1, "reason": "x", "close": 100}],
         "closed": [], "events": [], "decided": []}
    for d in days:
        swing.step_portfolio(s, W, d, {})
    tr = s["closed"][0]
    assert tr["exit_due"] == "trailing stop"                                    # 120 → stop 108; close 107 → sell
    assert tr["exit"] >= 100                                                    # no loss once trailing
