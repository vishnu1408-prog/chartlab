"""Live footprint recorder for NIFTY (index chart) from Angel One's tick feed — market data only, never orders.

Subscribes (SmartWebSocketV2, Snap Quote) to all live NIFTY futures and (LTP) to the NIFTY index. For every futures
update with new traded volume:
  traded = Δ(volume traded today) ÷ lot   (contracts — same unit as ChartLab's NIFTY volume)
  side   = BUY if the trade price ≥ the previous best ask, SELL if ≤ the previous best bid, otherwise the tick rule
           (up-tick → buy, down-tick → sell, unchanged → the previous side). An ESTIMATE: the feed sends snapshots,
           not every exchange trade with its aggressor flag (unlike Binance).
  price  = futures price − that contract's basis (smoothed futures − index), so the footprint sits on the INDEX
           candles the user trades.
Rows (minute IST, price level 0.5, buy, sell) are buffered and merged into data/footprint/NIFTY_50/<date>.parquet
every 20 s (footprint._merge_save sums increments). Runs 09:14–15:31 on NSE trading days; reconnects on drop.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections import defaultdict

import pandas as pd

from . import config

log = logging.getLogger("chartlab.footprint_rec")
SYM, ROOT, LEVEL = "NIFTY 50", "NIFTY", 0.5
status = {"state": "idle", "ticks": 0, "trades": 0, "contracts": 0.0, "since": None, "last": None, "error": ""}
_thread = None
_stop = threading.Event()


def _now():
    return dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)


class _Recorder:
    def __init__(self, futs, idx_token):
        self.futs = {str(tok): lot for _exp, tok, _s, lot in futs}
        self.idx_token = str(idx_token)
        self.idx = None                       # latest index LTP
        self.prev = {}                        # token → {"vol", "bid", "ask", "ltp", "side"}
        self.basis = {}                       # token → smoothed futures − index
        self.buf = defaultdict(lambda: [0.0, 0.0])
        self.lock = threading.Lock()

    def on_data(self, _ws, m):
        try:
            tok = str(m.get("token"))
            ltp = (m.get("last_traded_price") or 0) / 100.0
            status["ticks"] += 1
            if tok == self.idx_token:
                if ltp > 0:
                    self.idx = ltp
                return
            lot = self.futs.get(tok)
            if lot is None or ltp <= 0:
                return
            vol = float(m.get("volume_trade_for_the_day") or 0)
            bids = [x["price"] / 100.0 for x in (m.get("best_5_buy_data") or []) if x.get("price")]
            asks = [x["price"] / 100.0 for x in (m.get("best_5_sell_data") or []) if x.get("price")]
            p = self.prev.get(tok)
            if self.idx:
                b = ltp - self.idx
                self.basis[tok] = b if tok not in self.basis else 0.98 * self.basis[tok] + 0.02 * b
            if p is not None and vol > p["vol"] and tok in self.basis:
                qty = (vol - p["vol"]) / max(lot, 1)
                if p["ask"] and ltp >= p["ask"]:
                    side = 1
                elif p["bid"] and ltp <= p["bid"]:
                    side = -1
                elif ltp != p["ltp"]:
                    side = 1 if ltp > p["ltp"] else -1
                else:
                    side = p["side"] or 1
                ts = m.get("exchange_timestamp")
                t = (pd.Timestamp(ts, unit="ms") + pd.Timedelta(hours=5, minutes=30)) if ts else pd.Timestamp(_now())
                minute = t.floor("min")
                lv = round((ltp - self.basis[tok]) / LEVEL) * LEVEL
                with self.lock:
                    self.buf[(minute, lv)][0 if side > 0 else 1] += qty
                status["trades"] += 1
                status["contracts"] += qty
                status["last"] = str(minute)
            else:
                side = p["side"] if p else 0
            self.prev[tok] = {"vol": vol, "bid": bids[0] if bids else None, "ask": asks[0] if asks else None,
                              "ltp": ltp, "side": side}
        except Exception as e:                   # never let one odd packet kill the feed
            status["error"] = f"parse: {e}"

    def flush(self):
        from . import footprint as fp
        with self.lock:
            items, self.buf = list(self.buf.items()), defaultdict(lambda: [0.0, 0.0])
        if not items:
            return
        df = pd.DataFrame([(k[0], k[1], v[0], v[1]) for k, v in items], columns=["minute", "price", "buy", "sell"])
        for day, g in df.groupby(df.minute.dt.date):
            fp._merge_save(SYM, day, g.reset_index(drop=True))


def _session(api, creds):
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
    from . import futvol
    from .sources import Angel
    futs = futvol.contracts(SYM)
    idx_tok = Angel.token(SYM)
    if not futs or not idx_tok:
        raise RuntimeError("NIFTY futures / index token not found")
    rec = _Recorder(futs, idx_tok)
    ws = SmartWebSocketV2(api.access_token, creds["ANGEL_API_KEY"], creds["ANGEL_CLIENT_CODE"], api.getfeedToken(),
                          max_retry_attempt=3)

    def on_open(_w):
        ws.subscribe("cl_fp_fut", 3, [{"exchangeType": 2, "tokens": [str(f[1]) for f in futs]}])
        ws.subscribe("cl_fp_idx", 1, [{"exchangeType": 1, "tokens": [str(idx_tok)]}])
        status["state"] = "recording"

    def on_error(*a):
        status["error"] = "websocket error"

    ws.on_open, ws.on_data, ws.on_error = on_open, rec.on_data, on_error
    ws.on_close = lambda *a: None
    th = threading.Thread(target=ws.connect, daemon=True)
    th.start()
    end = _now().replace(hour=15, minute=31, second=0, microsecond=0)
    while not _stop.is_set() and _now() < end and th.is_alive():
        time.sleep(20)
        rec.flush()
    try:
        ws.close_connection()
    except Exception:
        pass
    rec.flush()


def _run():
    from .sources import Angel
    from . import sync
    status["since"] = str(_now())
    while not _stop.is_set():
        t = _now()
        m = t.hour * 60 + t.minute
        if t.weekday() < 5 and 9 * 60 + 14 <= m < 15 * 60 + 31 and not sync.is_holiday("NSE", t):
            api, creds = Angel.api(), Angel.creds()
            if api is None or creds is None:
                status.update(state="waiting for Angel login", error=Angel._last_error)
            else:
                try:
                    status["state"] = "connecting"
                    _session(api, creds)
                    status["state"] = "idle (session ended)"
                except Exception as e:
                    status.update(state="error", error=str(e)[:200])
                    log.warning("footprint recorder: %s", e)
            _stop.wait(15)                         # reconnect after a drop
        else:
            status["state"] = "idle (market closed)"
            _stop.wait(30)


def start():
    """Start the background recorder once (no-op offline or without Angel credentials)."""
    global _thread
    from .sources import Angel
    if config.OFFLINE or not Angel.available() or (_thread and _thread.is_alive()):
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="footprint-rec", daemon=True)
    _thread.start()
