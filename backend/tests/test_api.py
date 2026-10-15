import json
from pathlib import Path

import pytest

from app import config, sources, store, sync


def test_health_and_symbols(client):
    h = client.get("/api/health").json()
    assert h["ok"] and h["source"] == "offline"
    syms = {s["symbol"]: s for s in client.get("/api/symbols").json()}
    assert {"NIFTY 50", "NIFTY BANK", "TESTSTOCK"} <= set(syms)
    assert "file" not in syms["NIFTY 50"]


def test_candles_paging_and_validation(client):
    r = client.get("/api/candles", params={"symbol": "NIFTY 50", "tf": "15", "limit": 100}).json()
    assert len(r["bars"]) == 100 and r["has_more"] and r["last"] == r["bars"][-1]["time"]
    older = client.get("/api/candles", params={"symbol": "NIFTY 50", "tf": "15", "limit": 100, "before": r["bars"][0]["time"]}).json()
    assert older["bars"][-1]["time"] < r["bars"][0]["time"]
    t = r["bars"][10]["time"]
    fwd = client.get("/api/candles", params={"symbol": "NIFTY 50", "tf": "15", "start": t, "limit": 3}).json()
    assert [b["time"] for b in fwd["bars"]] == [b["time"] for b in r["bars"][10:13]]
    assert client.get("/api/candles", params={"symbol": "NIFTY 50", "tf": "7"}).status_code == 400
    assert client.get("/api/candles", params={"symbol": "XYZ", "tf": "5"}).status_code == 404


def test_indicator_compute_trims_to_window(client):
    bars = client.get("/api/candles", params={"symbol": "NIFTY 50", "tf": "5", "limit": 300}).json()["bars"]
    start, end = bars[0]["time"], bars[-1]["time"]
    for ind in client.get("/api/indicators").json()["indicators"]:
        out = client.post(f"/api/indicators/{ind['id']}/compute", json={"symbol": "NIFTY 50", "tf": "5", "start": start, "end": end}).json()
        assert "error" not in out, ind["id"]
        for s in out["series"]:
            assert all(start <= d["time"] <= end for d in s["data"]), ind["id"]
            assert len(s["data"]) == len(bars), ind["id"]                   # warm-up bars computed but not returned
        assert all(m["time"] >= start for m in out["markers"])
        assert out["markers"] == sorted(out["markers"], key=lambda m: m["time"])


def test_ema_value_uses_warmup_before_window(client):
    bars = client.get("/api/candles", params={"symbol": "NIFTY 50", "tf": "5", "limit": 50}).json()["bars"]
    out = client.post("/api/indicators/ema/compute", json={"symbol": "NIFTY 50", "tf": "5", "start": bars[0]["time"],
                                                           "end": bars[-1]["time"], "params": {"length": 21}}).json()
    full = store.bars("NIFTY 50", "5").close.ewm(span=21, adjust=False).mean().iloc[-50:]
    got = [d["value"] for d in out["series"][0]["data"]]
    assert max(abs(a - b) for a, b in zip(got, full)) < 1.0                 # settled thanks to the warm-up


def test_unknown_and_broken_indicators(client):
    assert client.post("/api/indicators/nope/compute", json={"symbol": "NIFTY 50", "tf": "5"}).status_code == 404
    p = Path(config.CUSTOM_DIR) / "boom.py"
    p.write_text("from app.indicators.base import Indicator\nclass Boom(Indicator):\n    id='boom'; name='Boom'\n"
                 "    def compute(self, df):\n        raise RuntimeError('kaput')\n")
    try:
        r = client.post("/api/indicators/boom/compute", json={"symbol": "NIFTY 50", "tf": "5"})
        assert r.status_code == 422 and "kaput" in r.json()["error"]
        assert client.get("/api/health").status_code == 200
    finally:
        p.unlink()


def test_drawings_roundtrip_and_path_safety(client):
    items = [{"id": "a", "type": "hline", "points": [{"time": 1, "price": 22000}], "color": "#fff"}]
    assert client.put("/api/drawings", params={"symbol": "NIFTY 50"}, json=items).json() == {"saved": 1}
    assert client.get("/api/drawings", params={"symbol": "NIFTY 50"}).json() == items
    client.put("/api/drawings", params={"symbol": "../../evil"}, json=items)
    assert not (Path(config.DATA_DIR).parent / "evil.json").exists()
    assert (Path(config.DATA_DIR) / "drawings" / "______evil.json").exists()
    assert client.get("/api/drawings", params={"symbol": "TESTSTOCK"}).json() == []


def test_journal(client):
    client.delete("/api/journal")
    client.post("/api/journal", json={"symbol": "NIFTY 50", "side": "BUY", "qty": 65, "pnl_rs": 650, "evil": "x"})
    j = client.get("/api/journal").json()
    assert len(j) == 1 and j[0]["pnl_rs"] == 650 and "evil" not in j[0]
    assert client.delete("/api/journal").json() == {"count": 0}


def test_static_frontend_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "ChartLab" in r.text
    assert client.get("/js/core.js").status_code == 200
    assert client.get("/vendor/lightweight-charts.standalone.production.js").status_code == 200


def test_live_websocket_status_and_bad_symbol(client):
    with client.websocket_connect("/ws/live?symbol=NIFTY%2050&tf=5") as ws:
        m = ws.receive_json()
        assert m["type"] == "status" and m["source"] == "offline"
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/live?symbol=XYZ&tf=5") as ws:
            ws.receive_json()


def test_live_hub_pushes_resampled_bars(client, monkeypatch):
    from conftest import synthetic_minutes
    today = synthetic_minutes(days=1, start="2030-01-07", seed=1)[:47]          # 47 minutes into the session
    monkeypatch.setattr(sync, "market_open", lambda t=None, sym=None: True)
    monkeypatch.setattr(sync, "latest_minutes", lambda sym: today)
    with client.websocket_connect("/ws/live?symbol=NIFTY%20BANK&tf=15") as ws:
        msgs = [ws.receive_json() for _ in range(2)]
    bars = next(m for m in msgs if m["type"] == "bars")["bars"]
    assert len(bars) == 3                                                      # last 3 bars; the 10:00 bar is still forming
    last = bars[-1]
    assert store.from_epoch(last["time"]) == today.index[0] + store.pd.Timedelta(minutes=45)
    assert last["close"] == round(today.close.iloc[-1], 2)


def test_sync_offline_and_yahoo_fallback(monkeypatch):
    assert sync.sync_symbol("NIFTY 50")["status"] == "offline"
    from conftest import synthetic_minutes
    fake1 = synthetic_minutes(days=1, start="2031-03-03", seed=2)
    monkeypatch.setattr(config, "OFFLINE", False)
    monkeypatch.setattr(sources, "yahoo", lambda sym, interval, period: fake1 if interval == "1m" else fake1.iloc[:0])
    r = sync.sync_symbol("NIFTY 50", force=True)
    assert r["status"] == "ok" and r["added"]["yahoo_1m"] == len(fake1)
    assert store.minute_bars("NIFTY 50").index[-1] == fake1.index[-1]


def test_angel_creds_parsing(tmp_path, monkeypatch):
    f = tmp_path / "creds.env"
    f.write_text("# comment\nANGEL_API_KEY=k\nANGEL_CLIENT_CODE=c\nANGEL_PIN=\nANGEL_TOTP_SECRET=s\n")
    monkeypatch.setattr(config, "ANGEL_CREDS", f)
    assert sources.Angel.creds() is None                                        # an empty value = not configured
    f.write_text("ANGEL_API_KEY=k\nANGEL_CLIENT_CODE=c\nANGEL_PIN=1234\nANGEL_TOTP_SECRET=s\n")
    assert sources.Angel.creds()["ANGEL_PIN"] == "1234"


def test_market_hours():
    import datetime as dt
    assert sync.market_open(dt.datetime(2026, 10, 7, 10, 0))                  # Wednesday
    assert not sync.market_open(dt.datetime(2026, 10, 10, 10, 0))             # Saturday
    assert not sync.market_open(dt.datetime(2026, 10, 7, 16, 0))


def test_market_hours_per_symbol():
    import datetime as dt
    assert sync.market_open(dt.datetime(2026, 10, 10, 10, 0), sym="BTCUSD")         # crypto: Saturday too
    assert sync.market_open(dt.datetime(2026, 10, 7, 23, 0), sym="XAUUSD")          # gold trades at night IST
    assert not sync.market_open(dt.datetime(2026, 10, 10, 20, 0), sym="XAUUSD")     # Saturday
