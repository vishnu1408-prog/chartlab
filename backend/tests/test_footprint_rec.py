from app.footprint_rec import _Recorder


def _q(tok, ltp, vol, bid, ask, ts=1763611200000):
    return {"token": tok, "last_traded_price": int(ltp * 100), "volume_trade_for_the_day": vol,
            "best_5_buy_data": [{"price": int(bid * 100)}], "best_5_sell_data": [{"price": int(ask * 100)}],
            "exchange_timestamp": ts}


def test_classifies_and_shifts_to_index():
    r = _Recorder([("2026-11-23", "111", "NIFTYFUT", 65)], "999")
    r.on_data(None, {"token": "999", "last_traded_price": 2500000})               # index 25,000.00
    r.on_data(None, _q("111", 25100.0, 1000, 25099.5, 25100.5))                  # first snapshot: baseline only
    r.on_data(None, _q("111", 25100.5, 1000 + 650, 25100.0, 25101.0))            # trade at previous ask → BUY 10
    r.on_data(None, _q("111", 25099.5, 1650 + 130, 25099.0, 25100.0))            # trade at previous bid → SELL 2
    rows = dict(r.buf)
    buy = sum(v[0] for v in rows.values()); sell = sum(v[1] for v in rows.values())
    assert round(buy, 6) == 10 and round(sell, 6) == 2
    levels = sorted(k[1] for k in rows)
    assert all(24990 < lv < 25010 for lv in levels)                              # futures 25,100 → index ≈ 25,000
