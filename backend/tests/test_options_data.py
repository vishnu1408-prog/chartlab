"""Option pricing for practice trades, Hugging Face spot data, and multi-resolution synced data."""
import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from app import config, options, store, symbols
from conftest import TMP, synthetic_minutes


def epoch(ts):
    return int(store.to_epoch(pd.DatetimeIndex([ts]))[0])


def test_black_scholes_parity_and_theta():
    S, K, T, s = 23600, 23600, 2 / 252, 0.14
    c, p = options.bs(S, K, T, s, True), options.bs(S, K, T, s, False)
    assert abs((c - p) - (S - K)) < 1e-6                               # put-call parity (r = 0)
    assert 0.35 * S * s * math.sqrt(T) < c < 0.45 * S * s * math.sqrt(T)   # ATM ≈ 0.4·S·σ·√T
    assert options.bs(S, K, 0.5 / 252, s, True) < c                    # less time → cheaper (theta)
    assert options.bs(S + 100, K, T, s, True) > c > options.bs(S - 100, K, T, s, True)


def test_years_to_expiry_counts_trading_days():
    t = pd.Timestamp("2024-06-25 15:30")                               # Tuesday close
    assert options.years_to_expiry(t, dt.date(2024, 6, 27)) == pytest.approx(2 / 252)
    t = pd.Timestamp("2024-06-21 09:15")                               # Friday open → Thursday: Fri + Mon..Thu
    assert options.years_to_expiry(t, dt.date(2024, 6, 27)) == pytest.approx(5 / 252)


def test_rule_expiries_follow_exchange_changes():
    nifty = options._rule_expiries("NIFTY", dt.date(2024, 6, 1), dt.date(2024, 6, 30))
    assert all(d.weekday() == 3 for d in nifty)                        # Thursday weeklies
    assert all(d.weekday() == 1 for d in options._rule_expiries("NIFTY", dt.date(2025, 10, 1), dt.date(2025, 10, 31)))
    monthly = options._rule_expiries("NIFTY", dt.date(2018, 5, 1), dt.date(2018, 5, 31))
    assert dt.date(2018, 5, 31) in monthly and len([d for d in monthly if d.month == 5]) == 1


def _write_chain(expiry, start, strike=20000, step=50):
    """Synthetic expiry file in the dataset's format (tz-aware timestamps)."""
    rows = []
    idx = pd.date_range(start, periods=375, freq="1min")
    for k in (strike - step, strike, strike + step):
        for typ, base in (("CE", 120.0), ("PE", 110.0)):
            px = base + np.arange(len(idx)) * 0.1 * (1 if typ == "CE" else -1) + (strike - k) * (0.5 if typ == "CE" else -0.5)
            rows.append(pd.DataFrame({"timestamp": idx.tz_localize("Asia/Kolkata"), "open": px, "high": px + 1, "low": px - 1,
                                      "close": px, "volume": 0, "open_interest": 0, "trading_day": idx.date, "symbol": "NIFTY",
                                      "strike": k, "option_type": typ, "expiry": str(expiry)}))
    p = options.chain_path("NIFTY", expiry)
    p.parent.mkdir(parents=True, exist_ok=True)
    pd.concat(rows).to_parquet(p)


def test_contract_uses_real_prices_when_available_and_model_otherwise(monkeypatch):
    nifty = store.bars("NIFTY 50", "1")
    day = nifty.index[200].normalize()
    expiry = (day + pd.offsets.Week(weekday=3)).date()
    monkeypatch.setattr(options, "dataset_expiries", lambda hf: [expiry])
    spot = float(nifty.close.iloc[200])
    k = round(spot / 50) * 50
    _write_chain(expiry, day + pd.Timedelta(minutes=555), strike=k)
    t = nifty.index[200]
    c = options.contract("NIFTY 50", epoch(t), "CE")
    assert c["source"] == "real" and c["strike"] == k and c["expiry"] == expiry.isoformat()
    assert c["bars"][0]["time"] == epoch(t) and c["premium"] == pytest.approx(120.0 + 200 * 0.1)
    assert store.from_epoch(c["bars"][-1]["time"]) == day + pd.Timedelta(minutes=929)      # marked until the close
    far = options.contract("NIFTY 50", epoch(t), "CE", strike=k + 1000)                     # strike not in the file
    assert far["source"] == "model" and far["premium"] > 0
    put = options.contract("NIFTY 50", epoch(t), "PE")
    assert put["source"] == "real" and put["premium"] == pytest.approx(110.0 - 20.0)
    with pytest.raises(ValueError):
        options.contract("NIFTY FIN SERVICE" if "NIFTY FIN SERVICE" in symbols.all_symbols() else "TESTSTOCK", epoch(t), "CE")


def test_model_premiums_move_with_the_underlying(monkeypatch):
    monkeypatch.setattr(options, "dataset_expiries", lambda hf: [])
    nifty = store.bars("NIFTY 50", "1")
    t = nifty.index[100]
    ce = options.contract("NIFTY 50", epoch(t), "CE")
    pe = options.contract("NIFTY 50", epoch(t), "PE")
    assert ce["source"] == pe["source"] == "model"
    spot = nifty.close.loc[t:].iloc[:len(ce["bars"])].to_numpy()
    cp = np.array([b["close"] for b in ce["bars"]])
    pp = np.array([b["close"] for b in pe["bars"]])
    assert np.corrcoef(spot, cp)[0, 1] > 0.9 and np.corrcoef(spot, pp)[0, 1] < -0.9
    for b in ce["bars"]:
        assert b["low"] <= min(b["open"], b["close"]) and b["high"] >= max(b["open"], b["close"])


def test_option_endpoint(client, monkeypatch):
    monkeypatch.setattr(options, "dataset_expiries", lambda hf: [])
    t = epoch(store.bars("NIFTY 50", "1").index[50])
    r = client.get("/api/option", params={"symbol": "NIFTY 50", "time": t, "type": "PE"})
    assert r.status_code == 200 and r.json()["type"] == "PE" and r.json()["bars"]
    assert client.get("/api/option", params={"symbol": "NIFTY 50", "time": t, "type": "XX"}).status_code == 400
    assert client.get("/api/option", params={"symbol": "TESTSTOCK", "time": t, "type": "CE"}).status_code == 400


def test_hugging_face_spot_fills_days_after_kaggle():
    s = symbols.get("NIFTY BANK")
    kaggle_last = store.load_base("NIFTY BANK").index[-1].normalize()
    extra = synthetic_minutes(days=2, start=str((kaggle_last + pd.offsets.BDay(1)).date()), seed=21, base=45500)
    overlap = synthetic_minutes(days=1, start=str(kaggle_last.date()), seed=22, base=99999)       # must NOT win
    hf = pd.concat([overlap, extra]).reset_index().rename(columns={"date": "timestamp"})
    hf["timestamp"] = hf.timestamp.dt.tz_localize("Asia/Kolkata")
    p = config.HF_DIR / "index" / "BANKNIFTY.parquet"
    hf.to_parquet(p)
    try:
        store._base_path("NIFTY BANK").unlink(missing_ok=True)
        merged = store.load_base("NIFTY BANK")
        assert merged.index[-1] == extra.index[-1]
        assert merged.loc[merged.index.normalize() == kaggle_last, "close"].max() < 90000      # Kaggle kept its own day
    finally:
        p.unlink()
        store._base_path("NIFTY BANK").unlink(missing_ok=True)


def test_hourly_and_daily_sync_fill_only_missing_days():
    sym = "NIFTY 50"
    last = store.minute_bars(sym).index[-1].normalize()
    d1 = (last + pd.offsets.BDay(3))
    hourly = store.resample(synthetic_minutes(days=2, start=str(d1.date()), seed=4), "60")
    daily = store.resample(synthetic_minutes(days=3, start=str((d1 + pd.offsets.BDay(5)).date()), seed=5), "D")
    store.save_ext(sym, hourly, "60")
    store.save_ext(sym, daily, "D")
    h = store.bars(sym, "60")
    assert h.index[-1].normalize() == hourly.index[-1].normalize()           # hourly data feeds 1h charts
    assert store.bars(sym, "15").index[-1].normalize() == last                # …but never 15m charts
    dd = store.bars(sym, "D")
    assert dd.index[-1] == daily.index[-1] and not dd.index.duplicated().any()
    assert dd.index.normalize().equals(dd.index)
