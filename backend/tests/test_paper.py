import datetime as dt

from app import paper

C = {"symbol": "NIFTY 50", "seg": "NFO", "token": "111", "tsym": "NIFTY20OCT2622800CE", "lot": 65,
     "expiry": "2026-10-20", "strike": 22800.0, "type": "CE"}
P = {**C, "token": "222", "tsym": "NIFTY20OCT2623200PE", "strike": 23200.0, "type": "PE"}
T0 = dt.datetime(2026, 10, 19, 9, 46)


def q(tok, bid, ask, ltp=None):
    return {tok: {"bid": bid, "ask": ask, "ltp": ltp or (bid + ask) / 2}}


def acct():
    return paper._new("manual")


_place = paper.place


def _place_with_note(st, order, *a, **k):
    order = {"note": "test reason", **order}
    return _place(st, order, *a, **k)


paper.place = _place_with_note


def test_charges_buy_and_sell_match_the_published_rates():
    c = paper.DEFAULT_SETTINGS["charges"]
    b = paper.charges("BUY", 300.0, 65, "NFO", c)              # turnover 19,500
    assert b["brokerage"] == 20 and b["stt"] == 0 and abs(b["stamp"] - 0.585) < 0.01
    assert abs(b["exchange"] - 19500 * 0.0003503) < 0.01
    assert abs(b["gst"] - 0.18 * (20 + b["exchange"] + b["sebi"])) < 0.01
    s = paper.charges("SELL", 300.0, 65, "NFO", c)
    assert abs(s["stt"] - 19.5) < 0.01 and s["stamp"] == 0
    assert paper.charges("BUY", 300.0, 20, "BFO", c)["exchange"] < paper.charges("BUY", 300.0, 20, "NFO", c)["exchange"]


def test_market_buy_fills_at_ask_and_cash_drops_by_premium_plus_charges():
    st = acct()
    rec = paper.place(st, {"contract": C, "side": "BUY", "lots": 1}, q("111", 299.5, 300.5), {"NIFTY 50": 23000.0}, T0)
    assert rec["status"] == "filled" and rec["fill_price"] == 300.5
    p = st["positions"][0]
    assert p["qty"] == 65 and p["avg"] == 300.5 and p["under_entry"] == 23000.0
    ch = paper.charges("BUY", 300.5, 65, "NFO", st["settings"]["charges"])["total"]
    assert abs(st["cash"] - (150_000 - 300.5 * 65 - ch)) < 1e-6


def test_rejections_market_closed_after_square_off_selling_unheld_and_cash():
    st = acct()
    qq = q("111", 299.5, 300.5)
    assert paper.place(st, {"contract": C, "side": "BUY", "lots": 1}, qq, {}, T0.replace(hour=8))["status"] == "rejected"
    assert "square-off" in paper.place(st, {"contract": C, "side": "BUY", "lots": 1}, qq, {}, T0.replace(hour=15, minute=16))["reason"]
    assert "only what you hold" in paper.place(st, {"contract": C, "side": "SELL", "lots": 1}, qq, {}, T0)["reason"]
    assert "not enough cash" in paper.place(st, {"contract": C, "side": "BUY", "lots": 8}, qq, {}, T0)["reason"]
    assert "lots" in paper.place(st, {"contract": C, "side": "BUY", "lots": 11}, qq, {}, T0)["reason"]
    assert not st["positions"] and st["cash"] == 150_000


def test_limit_buy_waits_then_fills_when_ask_drops():
    st = acct()
    rec = paper.place(st, {"contract": C, "side": "BUY", "lots": 1, "order_type": "LIMIT", "limit": 290},
                      q("111", 299.5, 300.5), {}, T0)
    assert rec["status"] == "open" and not st["positions"]
    assert paper.step(st, q("111", 294, 295), {}, T0 + dt.timedelta(minutes=1)) == 0
    assert paper.step(st, q("111", 288, 289), {}, T0 + dt.timedelta(minutes=2)) == 1
    assert st["positions"][0]["avg"] == 289 and rec["status"] == "filled"


def test_option_price_stop_sells_at_bid():
    st = acct()
    paper.place(st, {"contract": C, "side": "BUY", "lots": 1, "stop": {"basis": "option", "price": 270}},
                q("111", 299.5, 300.5), {}, T0)
    assert paper.step(st, q("111", 280, 281), {}, T0 + dt.timedelta(minutes=5)) == 0
    assert paper.step(st, q("111", 269, 271), {}, T0 + dt.timedelta(minutes=6)) == 1
    t = st["trades"][0]
    assert t["why"] == "stop" and t["exit"] == 269 and t["gross"] == round((269 - 300.5) * 65, 2)
    assert t["net"] < t["gross"]


def test_index_stop_on_a_put_triggers_when_the_index_RISES():
    st = acct()
    paper.place(st, {"contract": P, "side": "BUY", "lots": 1, "stop": {"basis": "index", "price": 23035}},
                q("222", 310, 311), {"NIFTY 50": 23000.0}, T0)
    assert paper.step(st, q("222", 300, 301), {"NIFTY 50": 22990.0}, T0 + dt.timedelta(minutes=1)) == 0
    assert paper.step(st, q("222", 290, 291), {"NIFTY 50": 23036.0}, T0 + dt.timedelta(minutes=2)) == 1
    assert st["trades"][0]["why"] == "stop" and st["trades"][0]["under_exit"] == 23036.0


def test_target_books_half_then_stop_moves_to_entry_index_level_then_square_off():
    st = acct()
    paper.place(st, {"contract": C, "side": "BUY", "lots": 2, "stop": {"basis": "index", "price": 22965},
                     "target": {"basis": "index", "price": 23035, "lots": 1}, "be_after_target": True},
                q("111", 299.5, 300.5), {"NIFTY 50": 23000.0}, T0)
    assert paper.step(st, q("111", 330, 331), {"NIFTY 50": 23036.0}, T0 + dt.timedelta(minutes=20)) == 1
    p = st["positions"][0]
    assert p["lots"] == 1 and p["stop"] == {"basis": "index", "price": 23000.0} and p["target"] is None
    assert paper.step(st, q("111", 340, 341), {"NIFTY 50": 23050.0}, T0.replace(hour=15, minute=15)) == 1
    t = st["trades"][0]
    assert t["why"].startswith("square-off") and [f["lots"] for f in t["fills"]] == [2, 1, 1]
    assert not st["positions"]


def test_optional_second_target_is_set_after_the_first_and_books_the_rest():
    st = acct()
    assert paper.target2(st) == {}                                 # off by default
    st["settings"]["me_target2"] = 70
    paper.place(st, {"contract": C, "side": "BUY", "lots": 2, "stop": {"basis": "index", "price": 22965},
                     "target": {"basis": "index", "price": 23035, "lots": 1}, "be_after_target": True, **paper.target2(st)},
                q("111", 299.5, 300.5), {"NIFTY 50": 23000.0}, T0)
    assert st["positions"][0]["target2"] == {"basis": "index", "price": 23070.0}
    assert paper.step(st, q("111", 330, 331), {"NIFTY 50": 23036.0}, T0 + dt.timedelta(minutes=20)) == 1
    p = st["positions"][0]
    assert p["stop"]["price"] == 23000.0 and p["target"] == {"basis": "index", "price": 23070.0} and "target2" not in p
    assert paper.step(st, q("111", 360, 361), {"NIFTY 50": 23071.0}, T0 + dt.timedelta(minutes=40)) == 1
    assert st["trades"][0]["why"] == "target" and not st["positions"]


def test_position_from_an_earlier_day_is_sold_at_first_live_price():
    st = acct()
    paper.place(st, {"contract": C, "side": "BUY", "lots": 1}, q("111", 299.5, 300.5), {}, T0)
    nxt = T0 + dt.timedelta(days=1, minutes=-30)                  # 09:16 next day
    assert paper.step(st, q("111", 250, 252), {}, nxt) == 1
    assert st["trades"][0]["why"].startswith("missed square-off")


def test_itm200_strikes():
    assert paper.itm200(23012, 1) == (22800, "CE")
    assert paper.itm200(23012, -1) == (23200, "PE")


def test_index_points_stop_and_target_are_set_from_the_index_at_the_fill():
    st = acct()
    paper.place(st, {"contract": P, "side": "BUY", "lots": 2, "stop": {"basis": "index_pts", "price": 35},
                     "target": {"basis": "index_pts", "price": 35, "lots": 1}}, q("222", 310, 311), {"NIFTY 50": 23000.0}, T0)
    p = st["positions"][0]
    assert p["stop"] == {"basis": "index", "price": 23035.0} and p["target"]["price"] == 22965.0 and p["target"]["lots"] == 1


B = {"symbol": "BTC", "seg": "DELTA", "token": "C-BTC-82000-201026", "tsym": "C-BTC-82000-201026", "lot": 1,
     "expiry": "2026-10-20", "strike": 82000.0, "type": "CE", "mult": 0.001}


def test_delta_fee_is_capped_at_3_5_pct_of_premium_plus_gst():
    c = paper.CRYPTO_SETTINGS["charges"]
    big = paper.charges("BUY", 2000.0, 10, "DELTA", c, 81600.0, 0.001)      # premium $20, 0.01% of $816 = $0.0816
    assert abs(big["fee"] - 0.0816) < 1e-6 and abs(big["total"] - 0.0816 * 1.18) < 1e-4
    cheap = paper.charges("BUY", 50.0, 10, "DELTA", c, 81600.0, 0.001)      # premium $0.50 → cap 3.5% = $0.0175
    assert abs(cheap["fee"] - 0.0175) < 1e-6


def test_btc_buy_on_a_saturday_uses_contract_size_and_usd_cash():
    st = paper._new("crypto")
    sat = dt.datetime(2026, 10, 17, 14, 0)
    rec = paper.place(st, {"contract": B, "side": "BUY", "lots": 10}, {B["token"]: {"bid": 1190, "ask": 1200, "ltp": 1195}},
                      {"BTC": 81600.0}, sat)
    assert rec["status"] == "filled"
    ch = paper.charges("BUY", 1200, 10, "DELTA", st["settings"]["charges"], 81600.0, 0.001)["total"]
    assert abs(st["cash"] - (1800 - 1200 * 10 * 0.001 - ch)) < 1e-9          # $12 premium for 10 contracts
    p = st["positions"][0]
    assert p["mult"] == 0.001 and p["qty"] == 10


def test_btc_position_sold_at_crypto_day_end_and_before_expiry():
    st = paper._new("crypto")
    t = dt.datetime(2026, 10, 18, 23, 0)                                       # Sunday 23:00 IST
    q1 = {B["token"]: {"bid": 1190, "ask": 1200, "ltp": 1195}}
    paper.place(st, {"contract": B, "side": "BUY", "lots": 5}, q1, {"BTC": 81600.0}, t)
    assert paper.step(st, q1, {"BTC": 81650.0}, t.replace(hour=23, minute=59) + dt.timedelta(hours=5, minutes=20)) == 0  # 05:19
    assert paper.step(st, q1, {"BTC": 81650.0}, dt.datetime(2026, 10, 19, 5, 31)) == 1
    assert st["trades"][-1]["why"].startswith("end of the crypto day")
    t2 = dt.datetime(2026, 10, 20, 17, 21)                                     # contract settles 17:30 IST
    assert "settles in under 10 minutes" in paper.place(st, {"contract": B, "side": "BUY", "lots": 1}, q1, {"BTC": 1.0}, t2)["reason"]
    paper.place(st, {"contract": B, "side": "BUY", "lots": 1}, q1, {"BTC": 81600.0}, dt.datetime(2026, 10, 20, 17, 0))
    assert paper.step(st, q1, {"BTC": 81600.0}, dt.datetime(2026, 10, 20, 17, 25)) == 1
    assert st["trades"][-1]["why"] == "5 min before expiry"


def test_delta_symbol_expiry_parsing():
    assert paper._delta_expiry("P-BTC-99000-271126") == dt.date(2026, 11, 27)


def test_closed_paper_trades_show_in_the_journal_with_qty_and_rupee_pnl():
    st = paper._new("crypto")
    t = dt.datetime(2026, 10, 18, 14, 0)
    q1 = {B["token"]: {"bid": 1190, "ask": 1200, "ltp": 1195}}
    paper.place(st, {"contract": B, "side": "BUY", "lots": 3}, q1, {"BTC": 81600.0}, t)
    paper.step(st, {B["token"]: {"bid": 1400, "ask": 1410, "ltp": 1405}}, {"BTC": 81600.0}, dt.datetime(2026, 10, 19, 5, 31))
    tr = st["trades"][-1]
    assert tr["qty"] == 3 and tr["mult"] == 0.001 and tr["currency"] == "USD"
    paper.save(st)
    rows = [r for r in paper.journal_rows() if r["id"] == f"paper-crypto-{tr['id']}"]
    assert rows and rows[0]["mode"] == "paper" and rows[0]["qty"] == 3 and rows[0]["pnl_pts"] == 200
    assert abs(rows[0]["pnl_rs"] - tr["net"] * 88.0) < 0.01 and rows[0]["tag"] == "Paper · crypto"
    paper.reset("crypto")


M = {"symbol": "MCX NATURALGAS", "seg": "MCX", "token": "444", "tsym": "NATURALGAS26OCT300CE", "lot": 1250,
     "expiry": "2026-10-23", "strike": 300.0, "type": "CE"}


def test_mcx_options_session_charges_expiry_guard_and_2300_square_off():
    st = paper._new("ng")
    qq = {"444": {"bid": 12.0, "ask": 12.2, "ltp": 12.1}}
    t = dt.datetime(2026, 10, 19, 9, 10)
    rec = paper.place(st, {"contract": M, "side": "BUY", "lots": 1, "stop": {"basis": "index", "price": 295.0}},
                      qq, {"MCX NATURALGAS": 302.0}, t)
    assert rec["status"] == "filled" and st["positions"][0]["qty"] == 1250
    sell = paper.charges("SELL", 12.0, 1250, "MCX", st["settings"]["charges"])
    assert abs(sell["stt"] - 12.0 * 1250 * 0.0005) < 0.01                       # CTT 0.05 % on the sell side
    assert paper.step(st, qq, {"MCX NATURALGAS": 301.0}, t.replace(hour=19)) == 0  # MCX still open at 19:00
    assert paper.step(st, qq, {"MCX NATURALGAS": 301.0}, t.replace(hour=23, minute=0)) == 1
    assert st["trades"][-1]["why"].startswith("square-off 23:00")
    exp_day = dt.datetime(2026, 10, 23, 10, 0)
    assert "expiry day" in paper.place(st, {"contract": M, "side": "BUY", "lots": 1}, qq, {}, exp_day)["reason"]
    assert "MCX closed" in paper.place(st, {"contract": M, "side": "BUY", "lots": 1}, qq, {}, t.replace(hour=8))["reason"]


def test_stock_options_are_listed_and_found(monkeypatch):
    from app import sources
    monkeypatch.setattr(sources.Angel, "token", classmethod(lambda cls, s: None))
    monkeypatch.setattr(sources.Angel, "_options", {
        ("NFO", "RELIANCE"): [(dt.date(2026, 10, 27), 1360.0, "CE", "1234", "RELIANCE27OCT261360CE", 500)],
        ("NFO", "NIFTY"): [(dt.date(2026, 10, 20), 23000.0, "CE", "9", "NIFTY..", 65)]})
    assert paper.stock_names() == ["RELIANCE"] and paper.is_stock("RELIANCE") and not paper.is_stock("NIFTY 50")
    c = paper.find("RELIANCE", "2026-10-27", 1360, "CE")
    assert c["seg"] == "NFO" and c["lot"] == 500 and c["tsym"] == "RELIANCE27OCT261360CE"
    st = paper._new("manual")
    rec = paper.place(st, {"contract": c, "side": "BUY", "lots": 1, "stop": {"basis": "index_pts", "price": 14}},
                      {"1234": {"bid": 20.0, "ask": 20.5, "ltp": 20.2}}, {"RELIANCE": 1365.0}, T0)
    assert rec["status"] == "filled" and st["positions"][0]["stop"] == {"basis": "index", "price": 1351.0}


def test_manual_buy_needs_a_reason_but_exits_and_bots_do_not():
    st = acct()
    qq = q("111", 299.5, 300.5)
    r = _place(st, {"contract": C, "side": "BUY", "lots": 1}, qq, {}, T0)
    assert r["status"] == "rejected" and "why" in r["reason"]
    assert _place(st, {"contract": C, "side": "BUY", "lots": 1, "note": "breakout"}, qq, {}, T0)["status"] == "filled"
    assert _place(st, {"contract": C, "side": "SELL", "lots": 1}, qq, {}, T0)["status"] == "filled"     # exit: no note
    b = paper._new("bot")
    assert _place(b, {"contract": C, "side": "BUY", "lots": 1, "source": "bot:x"}, qq, {}, T0)["status"] == "filled"
    t = st["trades"][-1]
    assert "plan0" in t and t["note"] == "breakout"


# ───────────── strategies with sold legs ─────────────
def _spread(st, t=T0, margin=10_000, **kw):
    """Bear call spread: sell 22800 CE (C, bid 300) and buy 23200 CE (token 333, ask 120)."""
    hi = {**C, "token": "333", "tsym": "NIFTY20OCT2623200CE", "strike": 23200.0}
    legs = [{"contract": C, "side": "SELL", "lots": 1}, {"contract": hi, "side": "BUY", "lots": 1}]
    qq = {**q("111", 300, 301), **q("333", 119, 120)}
    return paper.place_strategy(st, legs, qq, {"NIFTY 50": 23000.0}, t, "Bear call spread", margin,
                                kw.get("stop_rs"), kw.get("target_rs"), kw.get("note", "test spread")), hi


def test_strategy_sell_leg_collects_premium_and_blocks_margin():
    st = acct()
    r, hi = _spread(st)
    assert r["status"] == "filled"
    short = next(p for p in st["positions"] if p["token"] == "111")
    long_ = next(p for p in st["positions"] if p["token"] == "333")
    assert short["qty"] == -65 and short["lots"] == -1 and short["avg"] == 300      # sold at the bid
    assert long_["qty"] == 65 and long_["avg"] == 120                                # bought at the ask
    assert st["cash"] > 150_000 + (300 - 120) * 65 - 200                              # net credit in, minus charges
    assert paper.margin_used(st) == 10_000 and paper.free_cash(st) == st["cash"] - 10_000
    assert st["positions"][0]["token"] == "333"                                      # long leg filled first


def test_strategy_needs_a_note_quotes_and_unique_new_contracts():
    st = acct()
    assert "why" in _spread(st, note="")[0]["reason"]
    hi = {**C, "token": "333", "strike": 23200.0}
    r = paper.place_strategy(st, [{"contract": C, "side": "SELL", "lots": 1}, {"contract": hi, "side": "BUY", "lots": 1}],
                             q("111", 300, 301), {}, T0, "x", 0, None, None, "reason")
    assert "no live quote" in r["reason"] and not st["positions"] and not st.get("groups")
    assert "not enough cash" in _spread(st, margin=10_000_000)[0]["reason"]
    assert "square-off" in _spread(st, t=T0.replace(hour=15, minute=20))[0]["reason"]


def test_short_leg_marks_at_ask_and_strategy_stop_closes_all_legs():
    st = acct()
    _spread(st, stop_rs=3500)
    # NIFTY rallies: short 22800 CE ask 360 (−60×65 = −3900), long 23200 CE bid 135 (+15×65 = +975) → ≈ −2,925 − charges
    qq = {**q("111", 358, 360), **q("333", 135, 136)}
    assert paper.step(st, qq, {}, T0 + dt.timedelta(minutes=5)) == 0
    g = st["groups"][0]
    assert g["open"] and -3300 < g["pnl"] < -2925
    snap_unreal = sum(paper.mark(p) for p in st["positions"])
    assert abs(snap_unreal - (-(360 - 300) * 65 + (135 - 120) * 65)) < 1e-6
    qq = {**q("111", 378, 380), **q("333", 138, 139)}
    assert paper.step(st, qq, {}, T0 + dt.timedelta(minutes=6)) == 2
    assert not st["positions"] and not g["open"] and paper.margin_used(st) == 0
    short_t = next(t for t in st["trades"] if t["tsym"] == C["tsym"])
    assert short_t["side"] == "SHORT" and short_t["entry"] == 300 and short_t["exit"] == 380
    assert short_t["gross"] == round((300 - 380) * 65, 4) and short_t["why"] == "strategy stop"
    assert g["net"] == round(sum(t["net"] for t in st["trades"]), 2)
    assert abs(st["cash"] - (150_000 + g["net"])) < 0.01                              # cash = capital + net P&L


def test_strategy_target_and_square_off_buy_back_shorts():
    st = acct()
    _spread(st, target_rs=2000)
    qq = {**q("111", 250, 251), **q("333", 95, 96)}            # (300−251)×65 − (120−95)×65 = 1,560 → no
    assert paper.step(st, qq, {}, T0 + dt.timedelta(minutes=5)) == 0
    qq = {**q("111", 200, 201), **q("333", 80, 81)}            # 6,435 − 2,600 → target
    assert paper.step(st, qq, {}, T0 + dt.timedelta(minutes=9)) == 2
    assert st["groups"][0]["net"] > 2000
    st2 = acct()
    _spread(st2)
    assert paper.step(st2, {**q("111", 300, 302), **q("333", 119, 120)}, {}, T0.replace(hour=15, minute=15)) == 2
    t = next(t for t in st2["trades"] if t["side"] == "SHORT")
    assert t["exit"] == 302 and "square-off" in t["why"] and not st2["groups"][0]["open"]


def test_ticket_sell_is_a_one_leg_strategy_with_a_rupee_stop():
    """The Paper ticket's SELL places a one-leg strategy: premium received, margin blocked, stop in ₹ buys it back."""
    st = acct()
    r = paper.place_strategy(st, [{"contract": C, "side": "SELL", "lots": 1}], q("111", 300, 301), {"NIFTY 50": 23000.0},
                             T0, "Short 22800 CE", 120_000, 650, 1300, "practice selling")
    assert r["status"] == "filled"
    p = st["positions"][0]
    assert p["qty"] == -65 and p["avg"] == 300 and paper.margin_used(st) == 120_000
    assert paper.step(st, q("111", 305, 306), {}, T0 + dt.timedelta(minutes=3)) == 0      # −6 × 65 = −390 → hold
    assert paper.step(st, q("111", 309, 311), {}, T0 + dt.timedelta(minutes=6)) == 1      # −11 × 65 = −715 → stop
    t = st["trades"][0]
    assert t["side"] == "SHORT" and t["exit"] == 311 and t["why"] == "strategy stop" and paper.margin_used(st) == 0


def test_single_option_ticket_still_cannot_sell_to_open_and_cover_needs_no_note():
    st = acct()
    assert "only what you hold" in paper.place(st, {"contract": C, "side": "SELL", "lots": 1}, q("111", 300, 301), {}, T0)["reason"]
    _spread(st)
    r = _place(st, {"contract": C, "side": "BUY", "lots": 1, "source": "manual"}, q("111", 300, 302), {}, T0)
    assert r["status"] == "filled" and not any(p["token"] == "111" for p in st["positions"])


def test_option_points_stop_and_target_are_set_from_the_fill_price():
    st = acct()
    paper.place(st, {"contract": C, "side": "BUY", "lots": 2, "stop": {"basis": "option_pts", "price": 20},
                     "target": {"basis": "option_pts", "price": 30, "lots": 1}}, q("111", 299.5, 300.5), {}, T0)
    p = st["positions"][0]
    assert p["stop"] == {"basis": "option", "price": 280.5} and p["target"] == {"basis": "option", "price": 330.5, "lots": 1}
    assert paper.step(st, q("111", 280, 281), {}, T0 + dt.timedelta(minutes=1)) == 1 and not st["positions"]   # stop at bid


def test_limit_fill_converts_relative_levels_at_the_fill_not_at_the_order():
    st = acct()
    paper.place(st, {"contract": C, "side": "BUY", "lots": 1, "order_type": "LIMIT", "limit": 290,
                     "stop": {"basis": "index_pts", "price": 35}, "target": {"basis": "option_pts", "price": 25}},
                q("111", 299.5, 300.5), {"NIFTY 50": 23000.0}, T0)
    assert paper.step(st, q("111", 288, 289), {"NIFTY 50": 22990.0}, T0 + dt.timedelta(minutes=2)) == 1
    p = st["positions"][0]
    assert p["stop"] == {"basis": "index", "price": 22955.0} and p["target"] == {"basis": "option", "price": 314.0}


def test_modify_rejects_bad_levels(monkeypatch):
    import pytest
    st = acct()
    paper.place(st, {"contract": C, "side": "BUY", "lots": 1}, q("111", 299.5, 300.5), {}, T0)
    pid = st["positions"][0]["id"]
    monkeypatch.setattr(paper, "save", lambda s: None)
    monkeypatch.setattr(paper, "load", lambda a: st)
    with pytest.raises(ValueError):
        paper.modify("manual", pid, {"stop": {"basis": "option_pts", "price": 20}})
    assert paper.modify("manual", pid, {"stop": {"basis": "option", "price": 270}})["stop"] == {"basis": "option", "price": 270.0}


def test_morning_edge_bot_asks_for_the_nifty_price(monkeypatch):
    st = paper._new("bot")
    asked = []
    monkeypatch.setattr(paper, "load", lambda a: st)
    monkeypatch.setattr(paper, "save", lambda s: None)
    monkeypatch.setattr(paper, "quotes", lambda cs, want=(): (asked.append(tuple(want)), ({}, {}))[1])
    paper.bot_morning_edge({"signal": 1, "day": "2026-10-22"})
    assert asked and "NIFTY 50" in asked[0]


def test_morning_edge_bot_logs_a_no_trade_day_once(monkeypatch):
    st = paper._new("bot")
    monkeypatch.setattr(paper, "load", lambda a: st)
    monkeypatch.setattr(paper, "save", lambda s: None)
    for _ in range(2):
        paper.bot_morning_edge({"signal": 0, "pred": 0.031, "day": "2026-10-23"})
    assert [e["kind"] for e in st["events"]] == ["NO TRADE"]
    assert "+0.031" in st["events"][0]["text"] and not st["positions"]
