"""News store (RSS parsing, replay-safe queries, impact study) and the indicator backtester."""
import calendar
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from app import backtest, news, store


def ist(s):
    return calendar.timegm(pd.Timestamp(s).timetuple())


RSS = """<rss><channel>
<item><title><![CDATA[Sensex jumps 500 points as RBI holds repo rate]]></title><link>https://x.test/a</link>
<pubDate>Tue, 02 Jan 2024 10:05:00 +0530</pubDate><description>Markets cheer the &amp; policy</description></item>
<item><title>Rupee weakens against dollar</title><link>https://x.test/b</link><pubDate>Tue, 02 Jan 2024 04:50:00 GMT</pubDate></item>
<item><title>broken date</title><link>https://x.test/c</link><pubDate>not a date</pubDate></item>
</channel></rss>"""


def test_rss_parsing_converts_to_ist_and_dedupes():
    rows = news.parse_rss(RSS, "ET")
    assert [r["title"] for r in rows] == ["Sensex jumps 500 points as RBI holds repo rate", "Rupee weakens against dollar"]
    assert rows[0]["t"] == ist("2024-01-02 10:05") and rows[1]["t"] == ist("2024-01-02 10:20")   # 04:50 GMT = 10:20 IST
    assert rows[0]["summary"] == "Markets cheer the & policy"
    n1 = news.insert(rows)
    assert n1 == 2 and news.insert(rows) == 0                                                      # same link → stored once


def test_query_never_returns_future_headlines():
    news.insert([{"t": ist("2024-01-03 09:30"), "title": "Nifty opens flat", "url": "u1", "source": "T"},
                 {"t": ist("2024-01-03 11:00"), "title": "RBI governor speech", "url": "u2", "source": "T"},
                 {"t": ist("2024-01-03 14:00"), "title": "Nifty closes higher", "url": "u3", "source": "T"}])
    got = news.query(end=ist("2024-01-03 11:00"), start=ist("2024-01-03 00:00"))
    assert [g["title"] for g in got] == ["RBI governor speech", "Nifty opens flat"]                 # newest first, none after
    assert [g["title"] for g in news.query(q="rbi", start=ist("2024-01-03 00:00"))] == ["RBI governor speech"]


def test_news_api(client):
    r = client.get("/api/news", params={"end": ist("2024-01-02 10:10"), "start": ist("2024-01-02 00:00")}).json()
    assert any("Sensex jumps" in x["title"] for x in r) and all(x["time"] <= ist("2024-01-02 10:10") for x in r)
    assert client.post("/api/news/ensure", params={"start": "2024-01-01", "end": "x"}).status_code == 400
    st = client.get("/api/news/status").json()
    assert st["headlines"] >= 2 and st["offline"] is True
    assert client.get("/api/news/impact", params={"q": "a"}).status_code == 400


def test_ensure_days_queues_weekdays_with_priority():
    news._queue.clear()
    n = news.ensure_days(dt.date(2024, 1, 1), dt.date(2024, 1, 7), background=False)            # Mon..Sun
    assert n == 5 and news._queue[0] == dt.date(2024, 1, 1)
    news.ensure_days(dt.date(2024, 1, 5), dt.date(2024, 1, 5), background=False, priority=True)
    assert news._queue[0] == dt.date(2024, 1, 5) and len(news._queue) == 5
    news._queue.clear()


def test_impact_measures_moves_after_matching_headlines():
    m = store.minute_bars("NIFTY 50")
    day = m.index[0].normalize()
    t_news = day + pd.Timedelta(hours=10, minutes=2)
    news.insert([{"t": ist(t_news), "title": "Testword surprise announcement", "url": "imp1", "source": "T"},
                 {"t": ist(t_news + pd.Timedelta(minutes=3)), "title": "Testword follow-up", "url": "imp2", "source": "T"},   # same 15 min
                 {"t": ist(day + pd.Timedelta(hours=20)), "title": "Testword after hours", "url": "imp3", "source": "T"}]) # ignored
    r = news.impact("Testword", "NIFTY 50")
    assert r["n_headlines"] == 3 and len(r["events"]) == 1
    e = r["events"][0]
    p0 = m.close[m.index <= t_news].iloc[-1]
    p15 = m.close[m.index <= t_news + pd.Timedelta(minutes=15)].iloc[-1]
    assert e["ret_15"] == pytest.approx(round((p15 / p0 - 1) * 100, 3))
    assert r["summary"]["15"]["n"] == 1 and r["summary"]["15"]["normal_abs"] > 0


def test_backtest_rules_and_costs():
    P = {"fast": 3, "slow": 8}                       # fast EMAs: plenty of crosses on the trending synthetic data
    res = backtest.run("ema_cross", "NIFTY 50", "5", params=P, sl=1, tp=2, unit="atr", max_per_day=1, qty=65)
    tr = res["trades"]
    assert tr, "synthetic data should produce EMA-cross trades"
    days = pd.Series([store.from_epoch(t["entry_time"]).normalize() for t in tr])
    assert days.value_counts().max() == 1                                                         # max 1 per day
    for t in tr:
        a, b = store.from_epoch(t["entry_time"]), store.from_epoch(t["exit_time"])
        assert a.normalize() == b.normalize() and b > a                                           # never overnight
        assert 9 * 60 + 20 <= a.hour * 60 + a.minute <= 14 * 60 + 30
        assert t["reason"] in ("stop", "target", "15:15", "day end", "end of data")
        assert t["rs"] == pytest.approx(t["pts"] * 65, abs=1)
    for a, b in zip(tr, tr[1:]):
        assert b["entry_time"] > a["exit_time"] or b["entry_time"] >= a["exit_time"]              # one trade at a time
    s = res["summary"]
    assert s["trades"] == len(tr) and s["total_pts"] == pytest.approx(sum(t["pts"] for t in tr), abs=0.5)
    free = backtest.run("ema_cross", "NIFTY 50", "5", params=P, sl=1, tp=2, max_per_day=1, cost_pct=0)
    assert free["summary"]["total_pts"] > s["total_pts"]                                          # costs reduce the result
    longs = backtest.run("ema_cross", "NIFTY 50", "5", params=P, side="long")
    assert longs["trades"]
    assert all(t["dir"] == 1 for t in longs["trades"])


def test_backtest_api(client):
    r = client.post("/api/backtest/ema_cross", json={"symbol": "NIFTY 50", "tf": "15", "sl": 20, "tp": 40, "unit": "pts"})
    assert r.status_code == 200 and "summary" in r.json()
    assert client.post("/api/backtest/nope", json={"symbol": "NIFTY 50", "tf": "15"}).status_code == 404
