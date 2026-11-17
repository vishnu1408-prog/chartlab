"""Symbol registry: every NSE index and stock in the Kaggle files, SENSEX (BSE), BTC / gold, MCX commodities, and
their live-data tickers."""
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path

from . import config


@dataclass(frozen=True)
class Symbol:
    symbol: str
    kind: str            # "index" | "stock"
    file: str            # Kaggle 1-minute CSV ("" if none)
    yahoo: str           # "" = no Yahoo ticker (no Yahoo sync)
    angel_name: str      # name in Angel One's scrip master
    lot: int             # default quantity for practice trades
    exchange: str = "NSE"
    hf: str = ""         # Hugging Face 1-minute spot / option files (NIFTY / BANKNIFTY / SENSEX)
    options: bool = False
    session_start: int = config.SESSION_START    # minutes of the IST day; intraday bars are anchored here
    session_end: int = config.SESSION_END        # exclusive; 24-hour markets use 330 → 1770 (UTC days)
    binance: str = ""    # Binance spot pair (1-minute history archive + live klines), e.g. BTCUSDT
    note: str = ""       # shown in the symbol list (data source caveats)
    square_off: int = 0  # intraday square-off, minutes of the IST day (0 = NSE 15:15 / 24-hour markets 05:15)

    @property
    def nse_hours(self) -> bool:
        return (self.session_start, self.session_end) == (config.SESSION_START, config.SESSION_END)

    def public(self):
        d = asdict(self)
        d.pop("file")
        return d


# Yahoo ticker, Angel One name, lot, exchange, HF name, option practice supported
KNOWN = {
    "NIFTY 50": ("^NSEI", "Nifty 50", 65, "NSE", "NIFTY", True),
    "NIFTY BANK": ("^NSEBANK", "Nifty Bank", 30, "NSE", "BANKNIFTY", True),
    "SENSEX": ("^BSESN", "SENSEX", 20, "BSE", "SENSEX", True),
    "NIFTY FIN SERVICE": ("NIFTY_FIN_SERVICE.NS", "Nifty Fin Service", 60, "NSE", "", False),
    "INDIA VIX": ("^INDIAVIX", "India VIX", 1, "NSE", "", False),
    "NIFTY IT": ("^CNXIT", "Nifty IT", 1, "NSE", "", False),
    "NIFTY METAL": ("^CNXMETAL", "Nifty Metal", 1, "NSE", "", False),
    "NIFTY MIDCAP 100": ("NIFTY_MIDCAP_100.NS", "NIFTY MIDCAP 100", 1, "NSE", "", False),
    "NIFTY NEXT 50": ("^NSMIDCP", "Nifty Next 50", 1, "NSE", "", False),
    "NIFTY 100": ("^CNX100", "Nifty 100", 1, "NSE", "", False),
    "NIFTY 200": ("^CNX200", "Nifty 200", 1, "NSE", "", False),
    "NIFTY 500": ("^CRSLDX", "Nifty 500", 1, "NSE", "", False),
    "NIFTY AUTO": ("^CNXAUTO", "Nifty Auto", 1, "NSE", "", False),
    "NIFTY PHARMA": ("^CNXPHARMA", "Nifty Pharma", 1, "NSE", "", False),
    "NIFTY FMCG": ("^CNXFMCG", "Nifty FMCG", 1, "NSE", "", False),
    "NIFTY REALTY": ("^CNXREALTY", "Nifty Realty", 1, "NSE", "", False),
    "NIFTY PSU BANK": ("^CNXPSUBANK", "Nifty PSU Bank", 1, "NSE", "", False),
    "NIFTY ENERGY": ("^CNXENERGY", "Nifty Energy", 1, "NSE", "", False),
    "NIFTY MEDIA": ("^CNXMEDIA", "Nifty Media", 1, "NSE", "", False),
    "NIFTY INFRA": ("^CNXINFRA", "Nifty Infra", 1, "NSE", "", False),
}
# non-equity series in the index dataset (bonds, ETF NAVs, leveraged/TR variants) are left out
SKIP_PREFIX = ("BHARATBOND", "HANGSENG", "NIFTY GS", "NIFTY50 PR", "NIFTY50 TR", "NIFTY50 USD")
# 24-hour markets: IST timestamps; a day runs 05:30 → 05:30 IST (00:00 UTC, like TradingView), 1h bars at :30
GLOBAL = {
    "BTCUSD": Symbol("BTCUSD", "crypto", "", "BTC-USD", "", 1, "CRYPTO", session_start=330, session_end=1770,
                     binance="BTCUSDT", note="Bitcoin (Binance BTCUSDT 1-minute, live)"),
    "XAUUSD": Symbol("XAUUSD", "commodity", "", "GC=F", "", 1, "COMEX", session_start=330, session_end=1770,
                     note="Gold — COMEX futures GC=F (a few $ above spot); 1m last 7 days, 5m 60 days, 1h 2 years"),
}
# MCX commodity futures (Angel One): continuous near-month series in ₹, 1-minute history from 2016, daily from 2012.
# The session runs 09:00 → 23:30 (23:55 in the northern winter); the day is anchored at 09:00. Lot = the MINI
# contract's multiplier (₹ P&L per 1 point), the size a ₹1–2 lakh account can trade.
MCX = {
    "MCX GOLD": Symbol("MCX GOLD", "commodity", "", "", "GOLD", 10, "MCX", session_start=540, session_end=1440,
                       square_off=23 * 60, options=True, note="MCX Gold futures, ₹ per 10 g (Angel One) — lot ×10 = GOLDM 100 g"),
    "MCX SILVER": Symbol("MCX SILVER", "commodity", "", "", "SILVER", 5, "MCX", session_start=540, session_end=1440,
                         square_off=23 * 60, options=True, note="MCX Silver futures, ₹ per kg (Angel One) — lot ×5 = SILVERM 5 kg"),
    "MCX CRUDEOIL": Symbol("MCX CRUDEOIL", "commodity", "", "", "CRUDEOIL", 10, "MCX", session_start=540, session_end=1440,
                           square_off=23 * 60, options=True, note="MCX Crude oil futures, ₹ per barrel (Angel One) — lot ×10 = CRUDEOILM"),
    "MCX NATURALGAS": Symbol("MCX NATURALGAS", "commodity", "", "", "NATURALGAS", 250, "MCX", session_start=540,
                             session_end=1440, square_off=23 * 60, options=True,
                             note="MCX Natural gas futures, ₹ per mmBtu (Angel One) — lot ×250 = NATGASMINI"),
}


def mcx_dir(sym: str) -> Path:
    """1-minute history downloaded from Angel One, one parquet per year."""
    return config.DATA_DIR / "angel_hist" / sym.replace(" ", "_")


FIRST = ["NIFTY 50", "NIFTY BANK", "SENSEX", "NIFTY FIN SERVICE", "INDIA VIX"]


@lru_cache(maxsize=1)
def all_symbols() -> dict:
    out = {}
    idx_dir = Path(config.INDEX_DIR) if str(config.INDEX_DIR) not in ("", ".") else None
    names = set(KNOWN)
    if idx_dir and idx_dir.exists():
        names |= {f.name[: -len("_minute.csv")] for f in idx_dir.glob("*_minute.csv")}
    for name in sorted(names, key=lambda n: (FIRST.index(n) if n in FIRST else len(FIRST), n)):
        if name.startswith(SKIP_PREFIX):
            continue
        yh, an, lot, ex, hf, opt = KNOWN.get(name, ("", name, 1, "NSE", "", False))
        f = idx_dir / f"{name}_minute.csv" if idx_dir else None
        file = str(f) if f is not None and f.exists() else ""
        hf_ok = bool(hf) and (config.HF_DIR / "index" / f"{hf}.parquet").exists()
        if not file and not hf_ok and not (yh and not config.OFFLINE):
            continue                                   # no data source at all → don't list it
        out[name] = Symbol(name, "index", file, yh, an, lot, ex, hf, opt)
    first = {k: out[k] for k in FIRST if k in out}                 # NSE majors, then BTC / gold, then the rest
    from .sources import Angel                        # (sources imports this module)
    mcx = {k: v for k, v in MCX.items() if Angel.available() or mcx_dir(k).exists()}
    out = {**first, **GLOBAL, **mcx, **{k: v for k, v in out.items() if k not in first}}
    if str(config.STOCK_DIR) not in ("", ".") and Path(config.STOCK_DIR).exists():
        for f in sorted(Path(config.STOCK_DIR).glob("*_minute.csv")):
            s = f.name[: -len("_minute.csv")]
            if s in out:                       # the stock dataset also ships index files; keep the index dataset's
                continue
            out[s] = Symbol(s, "stock", str(f), s + ".NS", s, 1)
    return out


def get(symbol: str) -> Symbol:
    s = all_symbols().get(symbol)
    if s is None:                                    # an option contract, e.g. "NIFTY 27-Oct-26 23100 CE"
        from . import optsym
        s = optsym.symbol(symbol)
    if s is None:
        raise KeyError(f"unknown symbol {symbol!r}")
    return s
