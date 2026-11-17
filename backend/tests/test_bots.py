import datetime as dt

import numpy as np
import pandas as pd

from app import bots


def _hist_and_today(drive_body: float, sig_close: float):
    """10 quiet days (range 100 → ADR 100) + today: 09:15 bar body `drive_body`, 09:20 bar closes at `sig_close`."""
    rows = []
    for d in pd.bdate_range("2026-10-05", periods=10):
        for i in range(75):
            t = d + pd.Timedelta(minutes=555 + 5 * i)
            rows.append((t, 22500, 22550, 22450, 22500))
    today = pd.Timestamp("2026-10-19")
    o = 22500.0
    rows.append((today + pd.Timedelta(minutes=555), o, o + drive_body + 5, o - 5, o + drive_body))
    rows.append((today + pd.Timedelta(minutes=560), o + drive_body, sig_close + 2, o, sig_close))
    rows.append((today + pd.Timedelta(minutes=565), sig_close, sig_close + 3, sig_close - 3, sig_close))
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"]).set_index("date")


def test_gann_signal_on_strong_drive_and_close_beyond_level():
    up = (np.sqrt(22500) + 0.25) ** 2                       # ≈ 22575
    df = _hist_and_today(drive_body=40, sig_close=up + 10)  # body 40 ≥ 0.25 × 100
    g = bots.gann_signal(df, dt.datetime(2026, 10, 19, 9, 26))
    assert g and g["dir"] == 1 and g["sig_t"] == pd.Timestamp("2026-10-19 09:20") and g["entry_t"] == pd.Timestamp("2026-10-19 09:25")
    assert bots.gann_signal(_hist_and_today(drive_body=10, sig_close=up + 10), dt.datetime(2026, 10, 19, 9, 26)) is None  # weak
    assert bots.gann_signal(df, dt.datetime(2026, 10, 19, 9, 24)) is None   # the 09:20 bar has not closed yet


def test_daily_plan_priority():
    now = lambda h, m: dt.datetime(2026, 10, 19, h, m)
    early = {"dir": 1, "entry_t": pd.Timestamp("2026-10-19 09:25")}
    late = {"dir": -1, "entry_t": pd.Timestamp("2026-10-19 11:10")}
    me_yes, me_no = {"pred": -0.16}, {"pred": 0.03}
    assert bots.plan_choice(now(9, 26), early, None)[0] == "gann"          # Gann before 09:45 wins
    assert bots.plan_choice(now(9, 40), None, None)[0] is None             # wait for 09:45
    assert bots.plan_choice(now(9, 46), None, me_yes)[0] == "me"
    assert bots.plan_choice(now(9, 46), None, None)[0] is None             # decision still coming
    assert bots.plan_choice(now(11, 11), late, me_no)[0] == "gann"         # ME no trade → later Gann
    assert bots.plan_choice(now(11, 11), None, me_no)[0] is None


def _ng_bars(first_tod=540, body=6.0):
    rows = []
    for d in pd.bdate_range("2026-10-05", periods=10):
        for i in range(170):
            rows.append((d + pd.Timedelta(minutes=540 + 5 * i), 300, 310, 290, 300))   # ADR 20
    today = pd.Timestamp("2026-10-19")
    t0 = today + pd.Timedelta(minutes=first_tod)
    rows.append((t0, 300.0, 300 + body + 0.5, 299.5, 300 + body))
    rows.append((t0 + pd.Timedelta(minutes=5), 300 + body, 307.5, 305, 307))
    rows.append((t0 + pd.Timedelta(minutes=10), 307, 308, 306, 307.5))
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"]).set_index("date")


def test_ng_signal_needs_a_strong_first_bar_at_the_open():
    now = dt.datetime(2026, 10, 19, 9, 11)
    g = bots.ng_signal(_ng_bars(), now)                     # body 6 ≥ 0.25 × 20
    assert g and g["dir"] == 1 and g["entry_t"] == pd.Timestamp("2026-10-19 09:10") and abs(g["risk"] - 3.0) < 1e-9
    assert bots.ng_signal(_ng_bars(body=3.0), now) is None  # weak first bar
    assert bots.ng_signal(_ng_bars(first_tod=545), dt.datetime(2026, 10, 19, 9, 16)) is None   # data starts late


def test_no_trade_reason_explains_an_empty_day():
    up = (np.sqrt(22500) + 0.25) ** 2
    weak = bots.gann_today(_hist_and_today(drive_body=10, sig_close=up + 10), dt.datetime(2026, 10, 19, 9, 26))
    assert weak and not weak["strong"]
    assert "0.10×ADR (needs 0.25)" in bots.no_trade_reason("gann", weak, 9 * 60 + 26, 14 * 60 + 30)
    strong = bots.gann_today(_hist_and_today(drive_body=40, sig_close=22540), dt.datetime(2026, 10, 19, 9, 26))
    assert strong["strong"] and "sig" not in strong
    assert bots.no_trade_reason("gann", strong, 9 * 60 + 26, 14 * 60 + 30) is None       # may still come
    assert "no 5-minute close" in bots.no_trade_reason("gann", strong, 14 * 60 + 31, 14 * 60 + 30)
    assert bots.no_trade_reason("gann", None, 10 * 60, 14 * 60 + 30) is None             # 09:15 bar not seen yet


def test_note_no_trade_logs_once(monkeypatch):
    from app import paper
    st = paper._new("gann")
    monkeypatch.setattr(paper, "load", lambda a: st)
    monkeypatch.setattr(paper, "save", lambda s: None)
    for _ in range(3):
        bots.note_no_trade("gann", "Gann 2026-10-19", "weak", dt.datetime(2026, 10, 19, 9, 26))
    assert [e["text"] for e in st["events"]] == ["Gann 2026-10-19: weak"]
