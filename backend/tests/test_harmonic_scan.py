"""Pattern coach scanner: correct geometry, ratio checklist, no look-ahead, and challenge rounds."""
import json

import pandas as pd
import pytest

from app import harmonic_scan as hs, store


def _df():
    return store.bars("NIFTY 50", "5")


def test_zigzag_alternates_and_is_confirmed_later():
    P = hs.zigzag(_df(), 3)
    assert len(P) > 20
    assert all(P[i][2] != P[i + 1][2] for i in range(len(P) - 1))           # high, low, high, …
    assert all(conf >= bar + 3 for _, bar, _, conf in P)                     # known 3 bars after the swing


def test_checks_for_a_textbook_bat():
    # X 100, A 200 → B at 0.45 (bat 0.382–0.50), C at 0.618 of AB, D at 0.886 of XA
    x, a = 100.0, 200.0
    b = a - 0.45 * 100
    c = b + 0.618 * (a - b)
    d = a - 0.886 * 100
    cl, ok = hs.checks(hs.KEYS.index("bat"), x, a, b, c, d)
    assert ok and [y["leg"] for y in cl if not y.get("info")] == ["B", "C", "D"]
    assert "45.0% of XA" in cl[0]["text"] and cl[0]["need"] == "0.382–0.500"
    _, ok2 = hs.checks(hs.KEYS.index("gartley"), x, a, b, c, d)                # B 0.45 is not a Gartley (0.618)
    assert not ok2


def test_scan_finds_patterns_and_never_uses_the_future():
    df = _df()
    full = hs.scan(df, max_items=10_000)
    assert full["completed"], "synthetic data should contain harmonic patterns"
    for it in full["completed"]:
        assert all(c["ok"] for c in it["checks"] if not c.get("info"))
        t = [p["time"] for p in it["points"]]
        assert t == sorted(t) and it["known_at"] >= t[-1]
    cut = len(df) * 2 // 3
    part = hs.scan(df.iloc[:cut], max_items=10_000)
    cut_t = int(store.to_epoch(df.index[cut - 1: cut])[0])
    key = lambda it: (it["pattern"], tuple(p["time"] for p in it["points"]))
    known = {key(x) for x in full["completed"] if x["known_at"] <= cut_t}
    assert {key(x) for x in part["completed"]} == known                      # same patterns, none from after the cut
    for x in part["completed"]:
        r = x["reaction"]
        assert r is None or r["bars"] <= cut                                 # outcome only from data up to the cut


def test_forming_patterns_have_a_zone_and_are_not_invalidated():
    df = _df()
    for end in range(400, len(df), 97):
        r = hs.scan(df.iloc[:end])
        for f in r["forming"]:
            lo, hi = f["prz"]
            assert lo <= hi and len(f["points"]) in (3, 4)
            c = f["points"][-1]["price"]
            after = df.iloc[:end][df.index[:end] > store.from_epoch(f["points"][-1]["time"])]
            if f["dir"] == "bullish":
                assert after.high.max() <= c + 1e-6 if len(after) else True     # C not broken
            else:
                assert after.low.min() >= c - 1e-6 if len(after) else True


def test_challenge_round_structure_and_api(client):
    df = _df()
    after = int(store.to_epoch(df.index[200:201])[0])
    r = hs.next_setup(df, after)
    assert r is not None and r["start_time"] > after
    assert [p["label"] for p in r["points"]] == ["X", "A", "B", "C"]
    lo, hi = r["app"]["prz"]
    assert lo <= r["app"]["guess"] <= hi
    a = r["actual"]
    c = r["points"][-1]["price"]
    assert a["time"] >= r["points"][-1]["time"] and a["resolved_at"] > a["time"]
    assert (a["price"] <= c) if r["dir"] == "bullish" else (a["price"] >= c)    # D lies beyond C in the pattern's direction
    j = client.get("/api/harmonics/challenge", params={"symbol": "NIFTY 50", "tf": "5", "after": after}).json()
    assert j["start_time"] == r["start_time"]
    s = client.get("/api/harmonics/scan", params={"symbol": "NIFTY 50", "tf": "5", "bars": 300}).json()
    assert set(s) == {"completed", "forming"}
    end = int(store.to_epoch(df.index[500:501])[0])
    s2 = client.get("/api/harmonics/scan", params={"symbol": "NIFTY 50", "tf": "5", "end": end, "bars": 3000}).json()
    assert all(x["known_at"] < end for x in s2["completed"])


def test_study_day_and_busy_days(client):
    r = client.get("/api/harmonics/days", params={"symbol": "NIFTY 50", "tf": "5", "start": "2024-01-01", "end": "2024-12-31"}).json()
    assert r["total_patterns"] > 0 and r["top"]
    counts = [g["count"] for g in r["top"]]
    assert counts == sorted(counts, reverse=True)                                  # busiest first
    best = r["top"][0]
    d = client.get("/api/harmonics/day", params={"symbol": "NIFTY 50", "tf": "5", "date": best["date"]}).json()
    assert len(d["patterns"]) == best["count"]
    for p in d["patterns"]:
        assert store.from_epoch(p["points"][-1]["time"]).strftime("%Y-%m-%d") == best["date"]   # D on that day
    assert client.get("/api/harmonics/day", params={"symbol": "NIFTY 50", "date": "1999-01-01"}).status_code == 404
    assert client.get("/api/harmonics/day", params={"symbol": "NIFTY 50", "date": "nope"}).status_code == 400
    assert client.get("/api/harmonics/days", params={"symbol": "NIFTY 50", "start": "2015-01-01", "end": "2024-01-01"}).status_code == 400


def test_library_merges_patterns_and_filters(client):
    df = store.bars("NIFTY 50", "5")
    end = df.index[-1].strftime("%Y-%m-%d")
    start = (df.index[-1] - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    r = client.get("/api/harmonics/library", params={"symbol": "NIFTY 50", "tf": "5", "start": start, "end": end}).json()
    assert r["counts"]["total"] == r["matched"] > 0
    keys = [(g["dir"], tuple(p["time"] for p in g["points"])) for g in r["items"]]
    assert len(keys) == len(set(keys))                                         # one row per X-A-B-C-D
    for g in r["items"]:
        assert g["outcome"] in ("worked", "failed", "open") and set(g["patterns"]) == set(g["checks"])
        assert all(c["ok"] for n in g["patterns"] for c in g["checks"][n] if not c.get("info"))
    times = [g["points"][-1]["time"] for g in r["items"]]
    assert times == sorted(times, reverse=True)
    name = r["items"][0]["patterns"][0]
    f = client.get("/api/harmonics/library", params={"symbol": "NIFTY 50", "tf": "5", "start": start, "end": end, "pattern": name, "outcome": "worked"}).json()
    assert all(name in g["patterns"] and g["outcome"] == "worked" for g in f["items"])
    assert client.get("/api/harmonics/library", params={"symbol": "NIFTY 50", "start": "2015-01-01", "end": "2026-01-01"}).status_code == 400


def test_practice_log_roundtrip_and_whitelist(client):
    client.delete("/api/practice")
    client.post("/api/practice", json={"mode": "quiz", "pattern": "Bat", "answer": "Gartley", "ok": False, "evil": "x"})
    client.post("/api/practice", json={"mode": "plan", "status": "done", "R": 1.5, "ok": True})
    log = client.get("/api/practice").json()
    assert len(log) == 2 and "evil" not in log[0] and log[0]["at"] and log[1]["R"] == 1.5
    assert client.post("/api/practice", json={"note": "x" * 30000}).status_code == 413
    client.delete("/api/practice")
    assert client.get("/api/practice").json() == []
