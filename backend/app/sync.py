"""Fill the gap between the Kaggle history and today, and keep today's bars fresh."""
import datetime as dt
import logging
import threading
import time

import pandas as pd

from . import futvol, config, sources, store, symbols

log = logging.getLogger("chartlab.sync")
_last_sync: dict = {}
_running: set = set()
_lock = threading.Lock()


def now_ist() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=5, minutes=30)


def quote_time(q: dict | None) -> dt.datetime | None:
    """When the exchange last updated this quote (last trade, else last feed). Angel sends '19-Oct-2026 23:29:59';
    indices have no trades (trade time 01-Jan-1970) → their feed time."""
    if not q:
        return None
    for k in ("exchTradeTime", "exchFeedTime"):
        try:
            x = dt.datetime.strptime(str(q.get(k)), "%d-%b-%Y %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if x.year > 2000:
            return x
    return None


_holiday: dict = {}          # (exchange, date) → (checked at, closed?)
HOLIDAY_PROBE = {"NSE": "NIFTY 50", "BSE": "SENSEX", "MCX": "MCX CRUDEOIL"}
OPEN_MIN = {"NSE": 555, "BSE": 555, "MCX": 540}


def is_holiday(exchange: str, t: dt.datetime | None = None) -> bool:
    """A weekday the exchange did not open (e.g. Diwali): 10 minutes after the scheduled open its price feed has not
    updated today. Before that we don't know yet → False. Checked at most every 10 minutes; no Angel → False."""
    t = t or now_ist()
    if config.OFFLINE or t.weekday() >= 5 or exchange not in HOLIDAY_PROBE:
        return False
    if t.hour * 60 + t.minute < OPEN_MIN[exchange] + 10:
        return False
    key = (exchange, t.date())
    hit = _holiday.get(key)
    if hit and time.time() - hit[0] < 600:               # re-check every 10 min (special sessions, e.g. Muhurat)
        return hit[1]
    try:
        if not sources.Angel.available():
            return False
        sym = HOLIDAY_PROBE[exchange]
        if exchange == "MCX":
            from . import chain
            seg, name, rows = chain.contracts(sym)
            ex, tok, _ = chain.underlying(sym, seg, name, sorted({r[0] for r in rows})[0])
        else:
            ex, tok = exchange, sources.Angel.token(sym)
        q = sources.Angel.quotes({ex: [tok]}, "FULL").get(str(tok)) if tok else None
        qt = quote_time(q)
        if qt is None:
            return False
        # Angel sends feed heartbeats before the open even on holidays (24 Nov 2026: 06:40) → an update counts only if it
        # is after today's scheduled open.
        opened = dt.datetime.combine(t.date(), dt.time(OPEN_MIN[exchange] // 60, OPEN_MIN[exchange] % 60))
        closed = qt < opened
        _holiday[key] = (time.time(), closed)
        if closed:
            log.info("%s holiday detected: last exchange update %s", exchange, qt)
        return closed
    except Exception as e:
        log.warning("holiday check %s: %s", exchange, e)
        return False


def market_open(t: dt.datetime | None = None, sym: str | None = None) -> bool:
    t = t or now_ist()
    m = t.hour * 60 + t.minute
    s = symbols.all_symbols().get(sym) if sym else None
    if s is not None and s.kind == "crypto":
        return True                                            # trades 24/7
    if s is not None and s.exchange == "MCX":                  # Mon–Fri 09:00 → 23:30 (23:55 in the northern winter)
        return t.weekday() < 5 and 540 - 1 <= m <= 23 * 60 + 55 and not is_holiday("MCX", t)
    if s is not None and not s.nse_hours:                     # gold futures: Sun 18:00 → Fri 17:00 New York
        u = t - dt.timedelta(hours=5, minutes=30)              # UTC; closed Sat all day, Fri ≥ ~21:00, Sun < ~22:00 UTC
        return not (u.weekday() == 5 or (u.weekday() == 4 and u.hour >= 21) or (u.weekday() == 6 and u.hour < 22))
    return (t.weekday() < 5 and config.SESSION_START - 1 <= m <= config.SESSION_END
            and not is_holiday(s.exchange if s is not None and s.exchange in ("NSE", "BSE") else "NSE", t))


def nse_busy(t: dt.datetime | None = None) -> bool:
    """NSE trading hours (plus a few minutes either side), when the MCX history download stands aside."""
    t = t or now_ist()
    return t.weekday() < 5 and 9 * 60 <= t.hour * 60 + t.minute <= 15 * 60 + 35


def sync_symbol(sym: str, force: bool = False) -> dict:
    """Fetch bars after the last stored bar. Angel One: 1-minute from the last bar (any gap length).
    Yahoo fallback: 1-minute for the last 7 days and 5-minute for the last 60 days."""
    if config.OFFLINE:
        return {"symbol": sym, "status": "offline"}
    with _lock:
        if sym in _running:
            return {"symbol": sym, "status": "already running"}
        if not force and time.time() - _last_sync.get(sym, 0) < 600:
            return {"symbol": sym, "status": "fresh"}
        _running.add(sym)
    try:
        s = symbols.get(sym)
        added = {}
        if s.binance:                                            # crypto: monthly archives + REST for the rest
            added["binance_months"] = sources.binance_history(s.binance)
        have = store.minute_bars(sym)
        last = have.index.max() if len(have) else pd.Timestamp("2015-01-01")
        if s.binance:
            start = max(last + pd.Timedelta(minutes=1), pd.Timestamp(now_ist()) - pd.Timedelta(days=120))
            added["binance_1m"] = store.save_ext(sym, sources.binance_klines(s.binance, start.to_pydatetime()), "1")
            if not store.ext_path(sym, "D").exists() or force:
                added["yahoo_1d"] = store.save_ext(sym, sources.yahoo(sym, "1d", "max"), "D")   # daily before 2021
        elif sources.Angel.available() and s.kind == "option":     # option contract: ≤ 60 days of Angel candles
            start = max(last + pd.Timedelta(minutes=1), pd.Timestamp(now_ist()) - pd.Timedelta(days=60))
            added["angel_1m"] = store.save_ext(sym, sources.Angel.candles(sym, start.to_pydatetime(), now_ist()), "1")
        elif sources.Angel.available() and s.exchange == "MCX":
            backfill_async(sym)                                  # years of history: own thread, resumable
            start = max(last + pd.Timedelta(minutes=1), pd.Timestamp(now_ist()) - pd.Timedelta(days=20))
            added["angel_1m"] = store.save_ext(sym, sources.Angel.candles(sym, start.to_pydatetime(), now_ist()), "1")
        elif sources.Angel.available() and s.exchange in ("NSE", "BSE"):
            if sym in futvol.ROOTS and not market_open(sym=sym):
                futvol.backfill_async(sym)                       # index futures volume: history + today's top-up
            start = (last + pd.Timedelta(minutes=1)).to_pydatetime()
            new = sources.Angel.candles(sym, start, now_ist())
            added["angel_1m"] = store.save_ext(sym, new, "1")
        if not s.binance and not added.get("angel_1m") and s.yahoo:
            added["yahoo_1m"] = store.save_ext(sym, sources.yahoo(sym, "1m", "7d"), "1")
            added["yahoo_5m"] = store.save_ext(sym, sources.yahoo(sym, "5m", "60d"), "5")
            added["yahoo_60m"] = store.save_ext(sym, sources.yahoo(sym, "60m", "730d"), "60")
            if not store.ext_path(sym, "D").exists() or force:
                added["yahoo_1d"] = store.save_ext(sym, sources.yahoo(sym, "1d", "max"), "D")
        _last_sync[sym] = time.time()
        return {"symbol": sym, "status": "ok", "added": added, **sources.source_status()}
    except Exception as e:
        log.exception("sync %s", sym)
        return {"symbol": sym, "status": f"error: {e}"}
    finally:
        with _lock:
            _running.discard(sym)


def sync_async(sym: str, force: bool = False):
    threading.Thread(target=sync_symbol, args=(sym, force), daemon=True).start()


_live: dict = {}            # sym → {"day", "bars", "full_at", "fix_at", "vol"}: today's 1-minute bars kept between polls


def _tick(bars: pd.DataFrame, t: dt.datetime, price: float, vol: float = 0.0) -> pd.DataFrame:
    """Fold one live price into the current minute's bar (new bar at a new minute)."""
    m = pd.Timestamp(t).floor("min")
    if len(bars) and bars.index[-1] == m:
        r = bars.iloc[-1]
        bars.loc[m, ["high", "low", "close", "volume"]] = [max(r["high"], price), min(r["low"], price), price,
                                                           (r["volume"] or 0) + vol]
        return bars
    if len(bars) and bars.index[-1] > m:
        return bars
    row = pd.DataFrame({"open": [price], "high": [price], "low": [price], "close": [price], "volume": [vol]},
                       index=pd.DatetimeIndex([m], name="date"))
    return pd.concat([bars, row]) if len(bars) else row


def _angel_live(sym: str, t: dt.datetime, day_start: dt.datetime) -> pd.DataFrame:
    """Today's 1-minute bars from Angel without starving on the candle API's tight quota: the day's candles once,
    a short candle window once a minute to firm up closed bars (skipped while Angel is rate-limiting), and between
    those the live quote (a separate, generous limit) folded into the current minute every poll."""
    st = _live.get(sym)
    if st is None or st["day"] != t.date():
        st = _live[sym] = {"day": t.date(), "bars": store.empty(), "full_at": 0.0, "fix_at": 0.0, "vol": None,
                           "fcum": None, "fvol": {}}
    now = time.time()
    cooling = sources.Angel.cooling()
    if not cooling and now - st["fix_at"] >= 60:
        full = now - st["full_at"] > 30 * 60 or not len(st["bars"])
        a = day_start if full else max(day_start, t - dt.timedelta(minutes=15))
        d = sources.Angel.candles(sym, a, t, attempts=1)
        st["fix_at"] = now
        if len(d):
            st["full_at"] = now if full else st["full_at"]
            b = st["bars"]
            keep = b[(b.index < d.index[0]) | (b.index > d.index[-1])] if len(b) else b
            st["bars"] = pd.concat([keep, d[["open", "high", "low", "close", "volume"]]]).sort_index()
    s = symbols.get(sym)
    tok = sources.Angel.token(sym)
    futs = futvol.contracts(sym) if sym in futvol.ROOTS else []    # index: volume = all its futures (near+next+far)
    want = {s.exchange: [tok]} if tok else {}
    if futs:
        want["NFO"] = [f[1] for f in futs]
    qs = sources.Angel.quotes(want, "FULL") if want else {}
    q = qs.get(str(tok)) if tok else None
    fqs = [(qs.get(str(f[1])), f[3]) for f in futs]
    fqs = [(x, lot) for x, lot in fqs if x and x.get("tradeVolume") is not None
           and (quote_time(x) is None or quote_time(x).date() == t.date())]
    if fqs and len(fqs) == len(futs):                            # all contracts quoted: no false jumps
        cum = sum(float(x["tradeVolume"]) / max(lot, 1) for x, lot in fqs)   # shares → contracts (history unit)
        if st["fcum"] is not None:
            m = pd.Timestamp(t).floor("min")
            st["fvol"][m] = st["fvol"].get(m, 0.0) + max(cum - st["fcum"], 0.0)
        st["fcum"] = cum
    qt = quote_time(q)
    if q and qt is not None and qt.date() < t.date():
        q = None                                                 # stale (holiday / before the open): no fake bars
    if q and q.get("ltp"):
        cum = q.get("tradeVolume") or 0
        dv = max(cum - st["vol"], 0) if st["vol"] is not None else 0
        st["vol"] = cum
        st["bars"] = _tick(st["bars"], t, float(q["ltp"]), float(dv))
    out = st["bars"].copy()
    if st.get("fvol") and len(out):
        fv = pd.Series(st["fvol"]).reindex(out.index)
        out["volume"] = fv.where(fv.notna(), out["volume"])
    return out


def latest_minutes(sym: str) -> pd.DataFrame:
    """Today's 1-minute bars from the best live source (used by the live feed)."""
    if config.OFFLINE:
        return pd.DataFrame()
    t = now_ist()
    s = symbols.get(sym)
    if s.binance:                                                # the current UTC day (05:30 IST →)
        d0 = t.replace(hour=5, minute=30, second=0, microsecond=0)
        return sources.binance_klines(s.binance, d0 if t >= d0 else d0 - dt.timedelta(days=1))
    if s.exchange == "MCX":
        return _angel_live(sym, t, t.replace(hour=9, minute=0, second=0, microsecond=0))
    if not s.nse_hours:
        return sources.yahoo(sym, "1m", "2d")
    if sources.Angel.available():
        d = _angel_live(sym, t, t.replace(hour=9, minute=15, second=0, microsecond=0))
        if len(d):
            return d
    return sources.yahoo(sym, "1m", "1d")


# ───────────────────────────── MCX history (Angel One) ─────────────────────────────
# Downloaded newest → oldest so recent charts fill first; progress is saved after every block, so a restart resumes.
#   daily from 2012 → cache/ext D · 5-minute from 2016 → cache/ext 5 · 1-minute from 2016 → angel_hist/<sym>/1m_<year>
HIST = (("D", "ONE_DAY", "2012-01-01", 365), ("5", "FIVE_MINUTE", "2016-01-01", 120), ("1", "ONE_MINUTE", "2016-01-01", 28))
backfill_status: dict = {}
_backfilling: set = set()


def _state_path(sym):
    return symbols.mcx_dir(sym) / "progress.json"


def backfill(sym: str) -> dict:
    import json
    d = symbols.mcx_dir(sym)
    d.mkdir(parents=True, exist_ok=True)
    sp = _state_path(sym)
    state = json.loads(sp.read_text()) if sp.exists() else {}
    for res, interval, first, block in HIST:
        lo = pd.Timestamp(first)
        a_end = pd.Timestamp(state[res]) if res in state else pd.Timestamp(now_ist()).floor("min")
        while a_end > lo:
            while nse_busy():                                    # live NSE charts get Angel's candle quota first
                backfill_status[sym] = "history download paused during NSE hours (09:00–15:35), resumes after"
                time.sleep(60)
            a = max(lo, a_end - pd.Timedelta(days=block))
            backfill_status[sym] = f"downloading {'daily' if res == 'D' else res + '-minute'} history: {a.date()}"
            try:
                new = sources.Angel.candles(sym, a.to_pydatetime(), a_end.to_pydatetime(), interval, strict=True)
            except RuntimeError as e:                            # rate limit / timeouts: wait, retry the same block
                backfill_status[sym] = f"history download paused ({str(e)[:60]}), retrying"
                time.sleep(60)
                continue
            if res == "1":
                if len(new):
                    for y, part in store._clean(new, se=store.sess(sym)).groupby(lambda i: i.year):
                        f = d / f"1m_{y}.parquet"
                        old = pd.read_parquet(f) if f.exists() else store.empty()
                        store._clean(pd.concat([part, old]), se=store.sess(sym)).to_parquet(f)
            else:
                store.save_ext(sym, new, res)
            a_end = a - pd.Timedelta(minutes=1)
            state[res] = str(a_end if a_end > lo else lo)
            sp.write_text(json.dumps(state))
    backfill_status[sym] = "history complete"
    return state


def backfill_async(sym: str):
    with _lock:
        if sym in _backfilling or backfill_status.get(sym) == "history complete":
            return
        _backfilling.add(sym)

    def run():
        try:
            backfill(sym)
        except Exception as e:
            log.exception("backfill %s", sym)
            backfill_status[sym] = f"history download stopped: {e}"
        finally:
            with _lock:
                _backfilling.discard(sym)
    threading.Thread(target=run, daemon=True).start()
