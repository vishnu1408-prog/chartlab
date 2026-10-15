"""Recent / live market data. Angel One SmartAPI (market data only — never places orders) when the credentials file
is filled in; otherwise Yahoo Finance (free, ~1 minute delayed). Every call is optional: failures return empty data."""
import datetime as dt
import json
import logging
import threading
import time

import pandas as pd

from . import config, symbols

log = logging.getLogger("chartlab.sources")


# ───────────────────────────── Angel One ─────────────────────────────
# option chains kept from the scrip master (NSE / BSE index options, MCX options on futures)
OPTION_ROOTS = {("NFO", "NIFTY"), ("NFO", "BANKNIFTY"), ("NFO", "FINNIFTY"), ("BFO", "SENSEX"), ("MCX", "GOLD"),
                ("MCX", "SILVER"), ("MCX", "CRUDEOIL"), ("MCX", "NATURALGAS")}

class Angel:
    _lock = threading.Lock()
    _api = None
    _login_at = 0.0
    _tokens = None
    _tokens_day = None
    _options: dict = {}       # (exch_seg, name) → [(expiry, strike, "CE"/"PE", token, trading symbol, lot)]
    _futures: dict = {}       # MCX name → [(expiry, token, trading symbol)] (option underlyings)
    _last_error = ""

    @classmethod
    def creds(cls):
        c = {}
        try:
            for line in config.ANGEL_CREDS.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    c[k.strip()] = v.strip()
        except OSError:
            return None
        need = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET")
        return c if all(c.get(k) for k in need) else None

    @classmethod
    def available(cls) -> bool:
        return not config.OFFLINE and cls.creds() is not None

    @classmethod
    def api(cls):
        with cls._lock:
            if cls._api is not None and time.time() - cls._login_at < 6 * 3600:
                return cls._api
            c = cls.creds()
            if c is None or config.OFFLINE:
                return None
            try:
                import pyotp
                from SmartApi import SmartConnect
                api = SmartConnect(api_key=c["ANGEL_API_KEY"])
                # SmartAPI logs every failed request with its headers and body (session token, API key, even the PIN
                # on a failed login) to stdout and logs/<date>/app.log; its constructor re-arms that, so silence it here
                quiet = logging.getLogger("logzero_default")
                quiet.handlers.clear()
                quiet.setLevel(logging.CRITICAL + 1)
                quiet.propagate = False
                s = api.generateSession(c["ANGEL_CLIENT_CODE"], c["ANGEL_PIN"], pyotp.TOTP(c["ANGEL_TOTP_SECRET"]).now())
                if not s or not s.get("status"):
                    cls._last_error = f"login failed: {(s or {}).get('message', 'no response')}"
                    return None
                cls._api, cls._login_at, cls._last_error = api, time.time(), ""
                return api
            except Exception as e:                          # network, bad creds, library errors
                cls._last_error = f"login error: {e}"
                return None

    @classmethod
    def token(cls, sym: str):
        today = dt.date.today()
        if cls._tokens is None or cls._tokens_day != today:          # MCX near-month contract rolls → rebuild daily
            p = config.DATA_DIR / "cache" / "angel_scrip_master.json"
            try:
                if not p.exists() or time.time() - p.stat().st_mtime > 86400:
                    import requests
                    r = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json", timeout=60)
                    r.raise_for_status()
                    p.write_bytes(r.content)
                m = json.loads(p.read_text())
            except Exception as e:
                cls._last_error = f"scrip master: {e}"
                return None
            t, near, opts, futs = {}, {}, {}, {}
            for x in m:
                seg = x.get("exch_seg")
                name, s = x.get("name", ""), x.get("symbol", "")
                it = x.get("instrumenttype", "")
                if it in ("OPTIDX", "OPTFUT") and (seg, name) in OPTION_ROOTS:
                    try:
                        exp = dt.datetime.strptime(x.get("expiry", ""), "%d%b%Y").date()
                    except ValueError:
                        continue
                    if exp >= today:                         # strikes are in paise in the scrip master
                        opts.setdefault((seg, name), []).append(
                            (exp, float(x["strike"]) / 100, s[-2:], x["token"], s, int(float(x.get("lotsize") or 1))))
                    continue
                if seg == "MCX" and it == "FUTCOM":
                    try:
                        exp = dt.datetime.strptime(x.get("expiry", ""), "%d%b%Y").date()
                    except ValueError:
                        continue
                    if exp >= today:
                        futs.setdefault(name.upper(), []).append((exp, x["token"], s))
                    # the nearest live contract carries Angel's continuous history
                    if exp >= today and (name.upper() not in near or exp < near[name.upper()][0]):
                        near[name.upper()] = (exp, x["token"])
                    continue
                if seg not in ("NSE", "BSE"):
                    continue
                if x.get("instrumenttype") == "AMXIDX":           # e.g. name "NIFTY", symbol "Nifty 50": match either
                    t.setdefault((seg, name.upper()), x["token"])
                    t.setdefault((seg, s.upper()), x["token"])
                elif seg == "NSE" and s.endswith("-EQ"):
                    t[(seg, s[:-3].upper())] = x["token"]
            t.update({("MCX", k): v[1] for k, v in near.items()})
            cls._tokens, cls._tokens_day = t, today
            cls._options = opts
            cls._futures = {k: sorted(v) for k, v in futs.items()}
        sy = symbols.get(sym)
        return cls._tokens.get((sy.exchange, sy.angel_name.upper()))

    @classmethod
    def quotes(cls, by_exchange: dict, mode: str = "FULL") -> dict:
        """Live market quotes, {exchange: [tokens]} (≤ 50 tokens per request) → {token: quote}. Market data only."""
        api = cls.api()
        if api is None:
            return {}
        out = {}
        flat = [(ex, tok) for ex, toks in by_exchange.items() for tok in toks]
        for i in range(0, len(flat), 50):
            part = {}
            for ex, tok in flat[i:i + 50]:
                part.setdefault(ex, []).append(tok)
            try:
                r = api.getMarketData(mode, part)
                for q in ((r or {}).get("data") or {}).get("fetched") or []:
                    out[str(q.get("symbolToken"))] = q
                if not (r or {}).get("status"):
                    cls._last_error = f"quotes: {(r or {}).get('message')}"
            except Exception as e:
                cls._last_error = f"quotes: {e}"
            time.sleep(0.2)
        return out

    # Angel returns at most 8,000 candles per request and silently drops the OLDEST beyond that, so each request's
    # window must hold fewer bars than that: NSE ≈ 375 one-minute bars a day, MCX ≈ 870.
    STEP_DAYS = {("NSE", "ONE_MINUTE"): 29, ("MCX", "ONE_MINUTE"): 7, ("NSE", "FIVE_MINUTE"): 100,
                 ("MCX", "FIVE_MINUTE"): 40, ("NSE", "ONE_DAY"): 2000, ("MCX", "ONE_DAY"): 1000}
    _rate = threading.Lock()
    _last_call = 0.0
    calls = {"ok": 0, "denied": 0, "failed": 0}
    _cool_until = 0.0

    @classmethod
    def cooling(cls) -> bool:
        return time.time() < cls._cool_until

    @classmethod
    def _get(cls, api, params, attempts=4):
        """One historical request: shared rate limit (≈0.9/s across threads; Angel denies faster bursts) and retries on timeouts / rate denials."""
        for attempt in range(attempts):
            with cls._rate:                                   # every thread waits out a cool-down after a denial
                wait = max(1.1 - (time.time() - cls._last_call), cls._cool_until - time.time())
                if wait > 0:
                    time.sleep(wait)
                cls._last_call = time.time()
            try:
                r = api.getCandleData(params)
                if r and r.get("status"):
                    cls.calls["ok"] += 1
                    return r.get("data") or []
                cls._last_error = f"candles: {(r or {}).get('message')}"
            except Exception as e:
                cls._last_error = f"candles: {e}"
            denied = "access rate" in cls._last_error
            cls.calls["denied" if denied else "failed"] += 1
            log.warning("angel candles attempt %d: %s (%s)", attempt + 1, cls._last_error[-60:], cls.calls)
            if denied:                                         # Angel's quota resets per minute: back off globally
                cls._cool_until = max(cls._cool_until, time.time() + 30 * (attempt + 1))
            elif attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
        return None

    @classmethod
    def candles(cls, sym: str, start: dt.datetime, end: dt.datetime, interval="ONE_MINUTE", strict=False,
                attempts=4) -> pd.DataFrame:
        """strict: raise if any window failed (history downloads must not leave silent holes)."""
        api, tok = cls.api(), cls.token(sym)
        if api is None or tok is None:
            if strict:
                raise RuntimeError(cls._last_error or "Angel One not available")
            return pd.DataFrame()
        ex = symbols.get(sym).exchange
        out, a = [], start
        step = dt.timedelta(days=cls.STEP_DAYS.get(("MCX" if ex == "MCX" else "NSE", interval), 29))
        while a < end:
            b = min(a + step, end)
            rows = cls._get(api, attempts=attempts, params={"exchange": ex, "symboltoken": tok, "interval": interval,
                                  "fromdate": a.strftime("%Y-%m-%d %H:%M"), "todate": b.strftime("%Y-%m-%d %H:%M")})
            if rows is None and strict:
                raise RuntimeError(cls._last_error)
            out += rows or []
            a = b + dt.timedelta(minutes=1)
        if not out:
            return pd.DataFrame()
        d = pd.DataFrame(out, columns=["date", "open", "high", "low", "close", "volume"])
        d["date"] = pd.to_datetime(d["date"]).dt.tz_localize(None)
        return d.set_index("date")


# ───────────────────────────── Yahoo ─────────────────────────────
def yahoo(sym: str, interval: str, period: str) -> pd.DataFrame:
    if config.OFFLINE:
        return pd.DataFrame()
    if not symbols.get(sym).yahoo:
        return pd.DataFrame()
    try:
        import yfinance as yf
        x = yf.download(symbols.get(sym).yahoo, period=period, interval=interval, progress=False, auto_adjust=False)
    except Exception as e:
        log.warning("yahoo %s: %s", sym, e)
        return pd.DataFrame()
    if x is None or x.empty:
        return pd.DataFrame()
    x.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in x.columns]
    if x.index.tz is not None:
        x.index = x.index.tz_convert("Asia/Kolkata").tz_localize(None)
    x.index.name = "date"
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in x]
    return x[cols]


def source_status() -> dict:
    if config.OFFLINE:
        return {"source": "offline", "detail": "network disabled"}
    if Angel.available():
        return {"source": "angel", "detail": Angel._last_error or "Angel One SmartAPI (real time)"}
    return {"source": "yahoo", "detail": "Yahoo Finance (~1 min delayed). Fill ~/.config/angel/creds.env for Angel One."}


# ───────────────────────────── Binance (crypto, public data — no account, no key) ─────────────────────────────
BINANCE_FROM = "2021-01"          # first month of 1-minute history kept (memory: ~3M rows from 2021)


def binance_history(pair: str) -> int:
    """Download the monthly 1-minute kline archives (data.binance.vision) that are missing. Returns files fetched."""
    if config.OFFLINE:
        return 0
    import requests
    d = config.DATA_DIR / "binance" / pair
    d.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()
    y, m = map(int, BINANCE_FROM.split("-"))
    got = 0
    while (y, m) < (today.year, today.month):
        name = f"{pair}-1m-{y}-{m:02d}.zip"
        p = d / name
        if not p.exists():
            try:
                r = requests.get(f"https://data.binance.vision/data/spot/monthly/klines/{pair}/1m/{name}", timeout=120)
                if r.status_code == 200:
                    tmp = p.with_suffix(".part")
                    tmp.write_bytes(r.content)
                    tmp.rename(p)
                    got += 1
                else:
                    log.info("binance %s: HTTP %s", name, r.status_code)   # the newest month appears a few days late
            except Exception as e:
                log.warning("binance %s: %s", name, e)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return got


def binance_klines(pair: str, start: dt.datetime, end: dt.datetime | None = None) -> pd.DataFrame:
    """1-minute klines from the public REST API between IST times start and end (1000 per request)."""
    if config.OFFLINE:
        return pd.DataFrame()
    import requests
    ist = dt.timedelta(hours=5, minutes=30)
    a = int((start - ist).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    b = int(((end or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + ist) - ist).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
    rows = []
    for _ in range(200):                              # ≤ 200k minutes (~4.6 months) per call
        if a > b:
            break
        try:
            r = requests.get("https://api.binance.com/api/v3/klines",
                             params={"symbol": pair, "interval": "1m", "startTime": a, "endTime": b, "limit": 1000}, timeout=30)
            r.raise_for_status()
            k = r.json()
        except Exception as e:
            log.warning("binance klines %s: %s", pair, e)
            break
        if not k:
            break
        rows += k
        a = k[-1][0] + 60_000
        if len(k) < 1000:
            break
        time.sleep(0.1)
    if not rows:
        return pd.DataFrame()
    d = pd.DataFrame([x[:6] for x in rows], columns=["t", "open", "high", "low", "close", "volume"])
    d.index = pd.to_datetime(d.t.astype("int64"), unit="ms") + pd.Timedelta(hours=5, minutes=30)
    d.index.name = "date"
    return d.drop(columns="t").astype("float64")
