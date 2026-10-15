"""Morning Edge — a NIFTY intraday ML strategy built from every method tested on 2026-10-13 (data only, no orders).

What it combines (indicators/tests/mix.py, ml945_*.py): at 09:45 a gradient-boosting model reads
  · the 14 classic strategy signals (Gann ±1, opening drive, Dalton open drive, Crabel stretch, Camarilla, VIX ±½σ,
    overnight momentum, Supertrend, TWAP, RSI, gap, 15-min ORB, prev-day high/low, prev-day colour),
  · price context (move since the open, opening range, position in yesterday's range, distance to Gann levels …),
  · breadth of ~115 large caps (share above yesterday's close / their own VWAP, average return),
  · yesterday's NIFTY option OI (PCR, near-money PCR, OI bias, call / put walls, futures OI change), dealer gamma,
  · yesterday's FII / Pro / Client index-futures positioning, VIX level and its opening jump,
and predicts NIFTY's move from 09:45 to 15:15 in units of the 10-day average range (ADR).
Trade when |prediction| ≥ THRESH (0.10 lenient default; 0.15 was the stricter first choice): buy ITM CE / PE at 09:45; stop when NIFTY moves 35 pts
against; book half at +35 and move the stop on the rest to entry; square off 15:15.

Honest record (walk-forward, model always trained on earlier years only; real ITM-200 premiums 2022 → Jun 2026):
every threshold / exit combination tried was positive (PF 1.09–1.52), but 2023 and early 2026 were negative —
weak and unstable. Forward-test it on paper before risking money.
"""
import datetime as dt
import io
import json
import logging
import math
import pickle
import threading
import time
import zipfile

import numpy as np
import pandas as pd

from . import config, sources, store

log = logging.getLogger("chartlab.edge")
DIR = config.DATA_DIR / "edge"
SYM = "NIFTY 50"
THRESH, STRONG = 0.10, 0.20                 # |predicted move| in ADR units: trade (lenient default) / "strong"
STOP, BOOK = 35.0, 35.0                     # index pts: stop, first-half target
LIVE_FROM = pd.Timestamp("2026-10-14")      # first day the saved final model trades forward
TRAIN_UNTIL = "2026-10-12"                  # final model: everything up to here; later days are out of sample
VOTES = ["gann", "drive", "dalton", "crabel", "cam", "vix", "mom", "st", "twap", "rsi", "gap", "orb", "pdhl", "prevday"]
FEATURES = VOTES + ["nr7", "vixchg", "or_adr", "mv_adr", "gap_adr", "body_adr", "or30_adr", "pos_prev", "g1_adr",
                    "gm1_adr", "prev_ret_adr", "prev_rng_adr", "ret5_adr", "rsi14", "twap_adr", "br_up", "br_vwap",
                    "br_ret", "oi_pcr", "oi_pcrNear", "oi_oiBias", "oi_futOIchg", "oi_futRet", "oi_dte",
                    "oi_callwall_adr", "oi_putwall_adr", "gex_sign", "gex_log", "fl_FII", "fl_FII_chg", "fl_Pro",
                    "fl_Pro_chg", "fl_Client", "fl_Client_chg", "vix_lvl", "vix_chg", "vix_sig_adr", "dow"]
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Referer": "https://www.nseindia.com/"}
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
_lock = threading.Lock()
status: dict = {"state": "idle"}


# ───────────────────────────── NSE end-of-day files (F&O bhavcopy, participant OI) ─────────────────────────────
def _get(url: str) -> bytes | None:
    import requests
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=40)
            if r.status_code == 404:
                return None
            if r.status_code == 200 and r.content:
                return r.content
        except Exception:
            pass
        time.sleep(2 + 3 * attempt)
    return None


def parse_bhav(raw: bytes, d: dt.date) -> pd.DataFrame:
    """NIFTY index futures + the nearest two option expiries from one F&O bhavcopy (old or UDiFF format)."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    df = pd.read_csv(z.open(z.namelist()[0]), low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    if "INSTRUMENT" in df.columns:
        df = df[(df.SYMBOL == "NIFTY") & df.INSTRUMENT.isin(["FUTIDX", "OPTIDX"])]
        out = pd.DataFrame(dict(kind=df.OPTION_TYP.where(df.INSTRUMENT == "OPTIDX", "FUT"),
                                expiry=pd.to_datetime(df.EXPIRY_DT, format="%d-%b-%Y"), strike=df.STRIKE_PR,
                                close=df.CLOSE, oi=df.OPEN_INT, chg_oi=df.CHG_IN_OI, volume=df.CONTRACTS, und=np.nan))
    else:
        df = df[(df.TckrSymb == "NIFTY") & df.FinInstrmTp.isin(["IDF", "IDO"])]
        out = pd.DataFrame(dict(kind=df.OptnTp.where(df.FinInstrmTp == "IDO", "FUT"), expiry=pd.to_datetime(df.XpryDt),
                                strike=df.StrkPric.fillna(0), close=df.ClsPric, oi=df.OpnIntrst,
                                chg_oi=df.ChngInOpnIntrst, volume=df.TtlTradgVol, und=df.UndrlygPric))
    opt = out[out.kind.isin(["CE", "PE"])]
    exps = sorted(opt.expiry.unique())[:2]
    out = pd.concat([out[out.kind == "FUT"], opt[opt.expiry.isin(exps)]])
    out["date"] = pd.Timestamp(d)
    return out


def fetch_bhav(d: dt.date) -> pd.DataFrame | None:
    old = f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d.year}/{MON[d.month - 1]}/fo{d.day:02d}{MON[d.month - 1]}{d.year}bhav.csv.zip"
    new = f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
    for u in ([new, old] if d >= dt.date(2024, 7, 8) else [old, new]):
        raw = _get(u)
        if raw:
            try:
                return parse_bhav(raw, d)
            except Exception as e:
                log.warning("bhavcopy %s: %s", d, e)
    return None


def fetch_participant(d: dt.date) -> pd.DataFrame | None:
    raw = _get(f"https://archives.nseindia.com/content/nsccl/fao_participant_oi_{d:%d%m%Y}.csv")
    if not raw or b"Client" not in raw:
        return None
    x = pd.read_csv(io.BytesIO(raw), skiprows=1)
    x.columns = [c.strip() for c in x.columns]
    x = x[x["Client Type"].astype(str).str.strip().isin(["Client", "DII", "FII", "Pro"])].copy()
    x["Client Type"] = x["Client Type"].astype(str).str.strip()
    x.insert(0, "date", pd.Timestamp(d))
    return x


def _fo_path():
    return DIR / "fo_NIFTY.parquet"


def _part_path():
    return DIR / "participant.parquet"


def load_fo() -> pd.DataFrame:
    p = _fo_path()
    if not p.exists():
        seed = DIR / "fo_NIFTY.csv.gz"                       # copied from indicators/data (2015 → )
        if not seed.exists():
            return pd.DataFrame(columns=["kind", "expiry", "strike", "close", "oi", "chg_oi", "volume", "und", "date"])
        f = pd.read_csv(seed, parse_dates=["date", "expiry"])
        f.to_parquet(p)
    return pd.read_parquet(p)


def load_participant() -> pd.DataFrame:
    p = _part_path()
    if not p.exists():
        seed = DIR / "participant_oi.csv"
        if not seed.exists():
            return pd.DataFrame(columns=["date", "Client Type", "Future Index Long", "Future Index Short"])
        x = pd.read_csv(seed, usecols=range(16), on_bad_lines="skip")
        x["date"] = pd.to_datetime(x["date"], errors="coerce")
        x = x.dropna(subset=["date"])
        for c in x.columns[2:]:
            x[c] = pd.to_numeric(x[c], errors="coerce")
        x.to_parquet(p)
    return pd.read_parquet(p)


def update_nse(days: list) -> dict:
    """Download the EOD files for the given trading days that are not stored yet (polite: one request at a time)."""
    fo, pa = load_fo(), load_participant()
    have_fo = set(pd.to_datetime(fo.date).dt.date) if len(fo) else set()
    have_pa = set(pd.to_datetime(pa.date).dt.date) if len(pa) else set()
    added = {"fo": 0, "participant": 0}
    new_fo, new_pa = [], []
    for d in days:
        if d not in have_fo:
            x = fetch_bhav(d)
            if x is not None and len(x):
                new_fo.append(x); added["fo"] += 1
            time.sleep(0.5)
        if d not in have_pa:
            x = fetch_participant(d)
            if x is not None and len(x):
                new_pa.append(x); added["participant"] += 1
            time.sleep(0.5)
    if new_fo:
        pd.concat([fo, *new_fo]).drop_duplicates(["date", "kind", "expiry", "strike"], keep="last").to_parquet(_fo_path())
    if new_pa:
        allp = pd.concat([pa, *[x[[c for c in pa.columns if c in x.columns]] for x in new_pa]])
        allp.drop_duplicates(["date", "Client Type"], keep="last").to_parquet(_part_path())
    return added


# ───────────────────────────── daily derivatives features ─────────────────────────────
def oi_features(fo: pd.DataFrame, spot: pd.Series) -> pd.DataFrame:
    """Per day (from that day's EOD files): call / put walls, PCR, near PCR, OI bias, futures OI change (oi_study.py)."""
    rows = []
    fo = fo.copy()
    fo["date"] = pd.to_datetime(fo.date); fo["expiry"] = pd.to_datetime(fo.expiry)
    for d, g in fo.groupby("date"):
        if d not in spot.index:
            continue
        S = spot[d]
        opt = g[g.kind.isin(["CE", "PE"]) & (g.expiry > d)]
        if opt.empty:
            continue
        ex = opt.expiry.min()
        o = opt[opt.expiry == ex]
        ce, pe = o[o.kind == "CE"], o[o.kind == "PE"]
        up, dn = ce[ce.strike >= S], pe[pe.strike <= S]
        if up.empty or dn.empty:
            continue
        near = o[(o.strike >= S * 0.97) & (o.strike <= S * 1.03)]
        nce, npe = near[near.kind == "CE"], near[near.kind == "PE"]
        tot = near.chg_oi.abs().sum()
        fut = g[g.kind == "FUT"].sort_values("expiry").head(1)
        rows.append(dict(date=d, callWall=up.loc[up.oi.idxmax(), "strike"], putWall=dn.loc[dn.oi.idxmax(), "strike"],
                         dte=(ex - d).days, pcr=pe.oi.sum() / max(ce.oi.sum(), 1), pcrNear=npe.oi.sum() / max(nce.oi.sum(), 1),
                         oiBias=(npe.chg_oi.sum() - nce.chg_oi.sum()) / tot if tot else 0.0,
                         futClose=fut.close.iloc[0] if len(fut) else np.nan, futOI=fut.oi.iloc[0] if len(fut) else np.nan))
    f = pd.DataFrame(rows).set_index("date").sort_index() if rows else pd.DataFrame()
    if len(f):
        f["futRet"] = f.futClose.pct_change()
        f["futOIchg"] = f.futOI.pct_change()
    return f


def gex_series(fo: pd.DataFrame, vix_close: pd.Series) -> pd.Series:
    """Dealer gamma exposure proxy (s3_filter.py): Σ gamma × OI × sign(call +, put −) × S² × 1 %."""
    fo = fo.copy()
    fo["date"] = pd.to_datetime(fo.date); fo["expiry"] = pd.to_datetime(fo.expiry)
    fut = fo[fo.kind == "FUT"].sort_values("expiry").groupby("date").close.first()
    opt = fo[fo.kind.isin(["CE", "PE"]) & (fo.oi > 0)].copy()
    opt["dte"] = (opt.expiry - opt.date).dt.days
    opt = opt[(opt.dte >= 0) & (opt.dte <= 35)]
    out = {}
    for d, g in opt.groupby("date"):
        S = g.und.dropna().iloc[0] if g.und.notna().any() else fut.get(d, np.nan)
        v = vix_close[:d]
        sig = v.iloc[-1] / 100 if len(v) else np.nan
        if not (np.isfinite(S) and np.isfinite(sig)):
            continue
        T = np.maximum(g.dte.values, 0.5) / 365
        K = g.strike.values
        d1 = (np.log(S / K) + 0.5 * sig ** 2 * T) / (sig * np.sqrt(T))
        gamma = np.exp(-0.5 * d1 ** 2) / np.sqrt(2 * math.pi) / (S * sig * np.sqrt(T))
        sign = np.where(g.kind.values == "CE", 1.0, -1.0)
        out[d] = float((gamma * g.oi.values * sign).sum() * S * S * 0.01)
    return pd.Series(out, name="gex").sort_index()


def flow_table(pa: pd.DataFrame) -> pd.DataFrame:
    pa = pa.copy()
    pa["net"] = (pa["Future Index Long"] - pa["Future Index Short"]) / (pa["Future Index Long"] + pa["Future Index Short"])
    return pa.pivot_table(index="date", columns="Client Type", values="net")


# ───────────────────────────── breadth (~115 large caps) ─────────────────────────────
BREADTH_STOCKS = ("ABB ADANIENSOL ADANIENT ADANIGREEN ADANIPORTS ADANIPOWER AMBUJACEM APOLLOHOSP ASIANPAINT ATGL AXISBANK "
                  "BAJAJ-AUTO BAJAJFINSV BAJAJHFL BAJAJHLDNG BAJFINANCE BANKBARODA BEL BHARTIARTL BHEL BOSCHLTD BPCL "
                  "BRITANNIA CANBK CGPOWER CHOLAFIN CIPLA COALINDIA CUMMINSIND DABUR DIVISLAB DLF DMART DRREDDY EICHERMOT "
                  "ENRIN ETERNAL GAIL GODREJCP GRASIM HAL HAVELLS HCLTECH HDFCAMC HDFCBANK HDFCLIFE HEROMOTOCO HINDALCO "
                  "HINDUNILVR HINDZINC HYUNDAI ICICIBANK ICICIGI ICICIPRULI INDHOTEL INDIGO INDUSINDBK INFY IOC IRCTC IRFC "
                  "ITC JINDALSTEL JIOFIN JSWENERGY JSWSTEEL KOTAKBANK LICI LODHA LT LTM MARUTI MAXHEALTH MAZDOCK M&M "
                  "MOTHERSON MUTHOOTFIN NAUKRI NESTLEIND NHPC NTPC ONGC PFC PIDILITIND PNB POWERGRID RECLTD RELIANCE "
                  "SBILIFE SBIN SHREECEM SHRIRAMFIN SIEMENS SOLARINDS SUNPHARMA TATACAP TATACONSUM TATAPOWER TATASTEEL TCS "
                  "TECHM TITAN TMCV TMPV TORNTPHARM TRENT TVSMOTOR ULTRACEMCO UNIONBANK UNITDSPR VBL VEDL WIPRO ZYDUSLIFE").split()


def breadth_history() -> pd.DataFrame:
    """09:40-bar breadth per day: research file (Kaggle 1-min stocks, 2015 → Apr 2026) + live snapshots after that."""
    out = pd.DataFrame(columns=["br_up", "br_vwap", "br_ret"])
    seed = DIR / "breadth_5m.csv.gz"
    if seed.exists():
        b = pd.read_csv(seed, parse_dates=["date"])
        b = b[(b.date.dt.hour == 9) & (b.date.dt.minute == 40) & (b.n > 0)]
        out = pd.DataFrame({"br_up": (b.up / b.n).values, "br_vwap": (b.vwapUp / b.n).values,
                            "br_ret": (b.dayRet / b.n).values}, index=b.date.dt.normalize().values)
    snaps = {}
    for f in sorted((DIR / "breadth").glob("*.json")) if (DIR / "breadth").exists() else []:
        s = json.loads(f.read_text())
        snaps[pd.Timestamp(s["day"])] = {k: s[k] for k in ("br_up", "br_vwap", "br_ret")}
    if snaps:
        out = pd.concat([out[~out.index.isin(list(snaps))], pd.DataFrame(snaps).T]).sort_index()
    return out


def breadth_snapshot(day: dt.date, save: bool = True) -> dict | None:
    """Live: Angel quotes of the breadth stocks at ~09:45 → share above prev close, above own VWAP, avg log return."""
    sources.Angel.token(SYM)
    T = sources.Angel._tokens or {}
    toks = [T[("NSE", s.upper())] for s in BREADTH_STOCKS if ("NSE", s.upper()) in T]
    if not toks:
        return None
    q = sources.Angel.quotes({"NSE": toks}, "FULL")
    rows = [(x.get("ltp"), x.get("close"), x.get("avgPrice")) for x in q.values()]
    rows = [(l, c, a) for l, c, a in rows if l and c and a]
    if len(rows) < 60:
        return None
    ltp, close, avg = (np.array(v, dtype=float) for v in zip(*rows))
    snap = {"day": str(day), "n": len(rows), "br_up": float((ltp > close).mean()), "br_vwap": float((ltp > avg).mean()),
            "br_ret": float(np.log(ltp / close).mean()), "at": dt.datetime.now().isoformat(timespec="seconds")}
    if save:
        (DIR / "breadth").mkdir(parents=True, exist_ok=True)
        (DIR / "breadth" / f"{day}.json").write_text(json.dumps(snap))
    return snap


# ───────────────────────────── 09:45 features (one row per day) ─────────────────────────────
def _supertrend(H, L, C, n=10, k=3.0):
    pc = np.roll(C, 1); pc[0] = C[0]
    tr = np.maximum(H - L, np.maximum(abs(H - pc), abs(L - pc)))
    atr = pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().to_numpy()
    mid = (H + L) / 2
    up, dn = mid - k * atr, mid + k * atr
    fu, fd, tr_ = up.copy(), dn.copy(), np.ones(len(C), int)
    for i in range(1, len(C)):
        fu[i] = max(up[i], fu[i - 1]) if C[i - 1] > fu[i - 1] else up[i]
        fd[i] = min(dn[i], fd[i - 1]) if C[i - 1] < fd[i - 1] else dn[i]
        tr_[i] = 1 if C[i] > fd[i - 1] else -1 if C[i] < fu[i - 1] else tr_[i - 1]
    return tr_


class Context:
    """Everything the 09:45 rows need, loaded once (training builds ~2,800 rows from one Context)."""

    def __init__(self, d5=None, m1=None, vix=None):
        self.d5 = d5 if d5 is not None else store.bars(SYM, "5")
        self.d5 = self.d5[self.d5.index >= "2014-06-01"]
        self.m1 = m1 if m1 is not None else store.bars(SYM, "1")
        self.vix = vix if vix is not None else store.bars("INDIA VIX", "D")
        d5 = self.d5
        self.O, self.H, self.L, self.C = (d5[k].to_numpy() for k in ("open", "high", "low", "close"))
        self.mins = (d5.index.hour * 60 + d5.index.minute).to_numpy()
        self.dk = d5.index.normalize()
        self.days = list(self.dk.unique())
        self.pos = pd.Series(np.arange(len(d5)), index=d5.index).groupby(self.dk).apply(lambda s: s.to_numpy()).to_dict()
        Dy = d5.groupby(self.dk).agg(o=("open", "first"), h=("high", "max"), l=("low", "min"), c=("close", "last"))
        Dy["rng"] = Dy.h - Dy.l
        Dy["adr"] = Dy.rng.rolling(10).mean().shift(1)
        Dy["stretch"] = np.minimum(Dy.h - Dy.o, Dy.o - Dy.l).rolling(10).mean().shift(1)
        Dy["nr7"] = (Dy.rng == Dy.rng.rolling(7).min()).shift(1).fillna(False).astype(float)
        self.Dy = Dy
        self.ST = _supertrend(self.H, self.L, self.C)
        dl = np.diff(self.C, prepend=self.C[0])
        up = pd.Series(np.maximum(dl, 0)).ewm(alpha=1 / 14, adjust=False).mean()
        dn = pd.Series(np.maximum(-dl, 0)).ewm(alpha=1 / 14, adjust=False).mean()
        self.RSI = (100 - 100 / (1 + up / dn.replace(0, 1e-9))).to_numpy()
        fo = load_fo()
        self.oi = oi_features(fo, Dy.c) if len(fo) else pd.DataFrame()
        self.gex = gex_series(fo, self.vix.close) if len(fo) else pd.Series(dtype=float)
        pa = load_participant()
        self.flows = flow_table(pa) if len(pa) else pd.DataFrame()
        self.breadth = breadth_history()

    def row(self, d: pd.Timestamp, live: dict | None = None) -> dict | None:
        """Features known at 09:45 on day d (None if the morning bars are incomplete). `live` may carry today's
        breadth snapshot and VIX open / prev close from Angel when the stores are not updated yet."""
        live = live or {}
        if d not in self.pos or self.days.index(d) < 15:
            return None
        k = self.days.index(d)
        p = self.days[k - 1]
        ix = self.pos[d]
        O, H, L, C, mins = self.O, self.H, self.L, self.C, self.mins
        if mins[ix[0]] != 555:
            return None
        pre = ix[mins[ix] < 585]
        if len(pre) != 6:
            return None
        j = pre[-1]
        Dy = self.Dy
        o, adr, px = O[ix[0]], Dy.adr[d], C[j]
        if not np.isfinite(adr) or adr <= 0:
            return None
        ph, pl, pc, po = Dy.h[p], Dy.l[p], Dy.c[p], Dy.o[p]
        sg = lambda x: int(np.sign(x))
        r = np.sqrt(o)
        g1, gm1 = (r + 0.25) ** 2, (r - 0.25) ** 2
        v = {}
        v["gann"] = 1 if px > g1 else -1 if px < gm1 else 0
        body = C[ix[0]] - o
        v["drive"] = sg(body) if abs(body) >= 0.25 * adr else 0
        mv = px - o
        v["dalton"] = (1 if (mv >= 0.35 * adr and L[pre].min() >= o - 0.02 * adr)
                       else -1 if (-mv >= 0.35 * adr and H[pre].max() <= o + 0.02 * adr) else 0)
        s_ = Dy.stretch[d]
        mm = self.m1[(self.m1.index >= d + pd.Timedelta(minutes=555)) & (self.m1.index < d + pd.Timedelta(minutes=585))]
        cr = 0
        for _, b in mm.iterrows():
            if b.high >= o + s_: cr = 1; break
            if b.low <= o - s_: cr = -1; break
        v["crabel"] = cr
        R = ph - pl
        H3, L3, H4, L4 = pc + 1.1 * R / 4, pc - 1.1 * R / 4, pc + 1.1 * R / 2, pc - 1.1 * R / 2
        v["cam"] = 1 if px > H4 else -1 if px < L4 else -1 if px > H3 else 1 if px < L3 else 0
        vp = live.get("vix_prev") or self.vix.close.asof(p)
        vo = live.get("vix_open") or (self.vix.open.asof(d) if d in self.vix.index else np.nan)
        sig = o * vp / 100 / np.sqrt(252)
        v["vix"] = 1 if px > o + 0.5 * sig else -1 if px < o - 0.5 * sig else 0
        v["mom"] = sg(px - pc)
        v["st"] = int(self.ST[j])
        tp = (H[ix[0]:j + 1] + L[ix[0]:j + 1] + C[ix[0]:j + 1]) / 3
        v["twap"] = sg(px - tp.mean())
        v["rsi"] = 1 if self.RSI[j] > 55 else -1 if self.RSI[j] < 45 else 0
        gap = o - pc
        v["gap"] = sg(gap) if abs(gap) >= 0.3 * adr else 0
        orb = ix[:3]
        v["orb"] = 1 if px > H[orb].max() else -1 if px < L[orb].min() else 0
        v["pdhl"] = 1 if px > ph else -1 if px < pl else 0
        v["prevday"] = sg(pc - po)
        f = dict(v)
        f["nr7"] = Dy.nr7[d]
        f["vixchg"] = vo / vp - 1 if vp and np.isfinite(vo) else np.nan
        f["or_adr"] = (H[orb].max() - L[orb].min()) / adr
        f["mv_adr"] = mv / adr
        f["gap_adr"] = gap / adr
        f["body_adr"] = body / adr
        f["or30_adr"] = (H[pre].max() - L[pre].min()) / adr
        f["pos_prev"] = (px - pl) / (ph - pl) if ph > pl else 0.5
        f["g1_adr"] = (px - g1) / adr
        f["gm1_adr"] = (px - gm1) / adr
        f["prev_ret_adr"] = (pc - po) / adr
        f["prev_rng_adr"] = (ph - pl) / adr
        f["ret5_adr"] = (pc - Dy.c[self.days[k - 6]]) / adr if k >= 6 else 0
        f["rsi14"] = self.RSI[j]
        f["twap_adr"] = (px - tp.mean()) / adr
        b = live.get("breadth") or (self.breadth.loc[d].to_dict() if d in self.breadth.index else {})
        for c in ("br_up", "br_vwap", "br_ret"):
            f[c] = b.get(c, np.nan)
        if p in self.oi.index:
            q = self.oi.loc[p]
            for c in ("pcr", "pcrNear", "oiBias", "futOIchg", "futRet", "dte"):
                f["oi_" + c] = q[c]
            f["oi_callwall_adr"] = (q.callWall - o) / adr
            f["oi_putwall_adr"] = (o - q.putWall) / adr
        if p in self.gex.index:
            f["gex_sign"] = float(np.sign(self.gex[p]))
            f["gex_log"] = float(np.sign(self.gex[p]) * np.log10(1 + abs(self.gex[p])))
        if p in self.flows.index:
            fp = self.flows.loc[p]
            i_ = self.flows.index.get_loc(p)
            for c in ("FII", "Pro", "Client"):
                f[f"fl_{c}"] = fp.get(c, np.nan)
                f[f"fl_{c}_chg"] = fp.get(c, np.nan) - self.flows.iloc[i_ - 1].get(c, np.nan) if i_ > 0 else np.nan
        f["vix_lvl"] = vp
        f["vix_chg"] = vo / vp - 1 if vp and np.isfinite(vo) else np.nan
        f["vix_sig_adr"] = sig / adr
        f["dow"] = d.weekday()
        f["_adr"] = adr
        return f

    def outcome(self, d: pd.Timestamp) -> float | None:
        """training target: (15:15 price − 09:45 price) / ADR, from 1-minute bars (5-minute bars on days without
        1m history — the 09:45 and 15:15 bar opens are the same price in both)"""
        a, b = d + pd.Timedelta(minutes=585), d + pd.Timedelta(minutes=915)
        m = self.m1[(self.m1.index >= a) & (self.m1.index <= b)]
        if len(m) >= 300 and m.index[0] == a and m.index[-1] == b:
            return float((m.open.iloc[-1] - m.open.iloc[0]) / self.Dy.adr[d])
        m = self.d5[(self.d5.index >= a) & (self.d5.index <= b)]
        if len(m) == 67 and m.index[0] == a and m.index[-1] == b:
            return float((m.open.iloc[-1] - m.open.iloc[0]) / self.Dy.adr[d])
        return None


def dataset(ctx: Context | None = None) -> pd.DataFrame:
    ctx = ctx or Context()
    rows = []
    for d in ctx.days:
        if d < pd.Timestamp("2015-02-01"):
            continue
        f = ctx.row(d)
        if f is None:
            continue
        f["y"] = ctx.outcome(d)
        rows.append(dict(day=d, **f))
    return pd.DataFrame(rows).set_index("day")


# ───────────────────────────── model ─────────────────────────────
def _model():
    from sklearn.ensemble import HistGradientBoostingRegressor
    return HistGradientBoostingRegressor(max_depth=3, learning_rate=0.05, max_iter=200, min_samples_leaf=50, random_state=0)


def model_path():
    return DIR / "model.pkl"


BREADTH = ["br_up", "br_vwap", "br_ret"]
NO_BREADTH = [c for c in FEATURES if c not in BREADTH]


def _fit_pair(X: pd.DataFrame, y: pd.Series) -> dict:
    """the full model plus a twin without breadth: days whose breadth is missing (no 1-minute stock history for
    May–Oct 2026, or a failed live snapshot) are predicted by the twin instead of a model that never saw a gap"""
    return {"model": _model().fit(X[FEATURES], y), "model_nobr": _model().fit(X[NO_BREADTH], y)}


def predict(M: dict, X: pd.DataFrame) -> tuple:
    """predictions and which model made each one ("full" / "no-breadth")"""
    has = X[BREADTH].notna().all(axis=1).to_numpy()
    out = np.full(len(X), np.nan)
    if has.any():
        out[has] = M["model"].predict(X.loc[has, FEATURES])
    if (~has).any():
        if "model_nobr" in M:
            out[~has] = M["model_nobr"].predict(X.loc[~has, NO_BREADTH])
        else:                                                       # model saved before the twin existed
            out[~has] = M["model"].predict(X.loc[~has, FEATURES])
    return out, np.where(has, "full", "no-breadth")


def train(ds: pd.DataFrame | None = None, until: str = TRAIN_UNTIL) -> dict:
    """Final model on every day up to `until`, plus walk-forward predictions (each year from a model trained only on
    earlier years; days after `until` from the final model) for the chart's history."""
    import sklearn
    ds = ds if ds is not None else dataset()
    from .sync import now_ist
    ds = ds[ds.index < pd.Timestamp(now_ist().date())]             # history = finished days only (today is live)
    ds.to_parquet(DIR / "dataset.parquet")
    t = ds[ds.y.notna()]
    y = t.y.clip(-3, 3)
    wf = pd.Series(np.nan, index=ds.index)
    for yr in range(2017, int(until[:4]) + 1):
        tr = (t.index.year < yr)
        te = (ds.index.year == yr) & (ds.index <= until)
        if te.any():
            wf[te] = predict(_fit_pair(t.loc[tr], y[tr]), ds.loc[te])[0]
    final_tr = t.index <= until
    M = _fit_pair(t.loc[final_tr], y[final_tr])
    after = ds.index > until
    if after.any():
        wf[after] = predict(M, ds.loc[after])[0]
    meta = {"features": FEATURES, "trained_until": until, "rows": int(final_tr.sum()), "sklearn": sklearn.__version__,
            "created": dt.datetime.now().isoformat(timespec="seconds"), "thresh": THRESH}
    with open(model_path(), "wb") as fh:
        pickle.dump({**M, "meta": meta}, fh)
    pd.DataFrame({"pred": wf, "source": np.where(ds.index > until, "final", "walk-forward")}).to_csv(DIR / "history.csv")
    _cache.clear()
    return meta


_cache: dict = {}


def load_model():
    if "m" not in _cache and model_path().exists():
        with open(model_path(), "rb") as fh:
            _cache["m"] = pickle.load(fh)
    return _cache.get("m")


def signals() -> pd.DataFrame:
    """Every day's prediction: walk-forward history + saved live signals (live overrides)."""
    out = pd.DataFrame(columns=["pred", "source"])
    h = DIR / "history.csv"
    if h.exists():
        out = pd.read_csv(h, parse_dates=[0], index_col=0)
    live = {}
    for f in sorted((DIR / "signals").glob("*.json")) if (DIR / "signals").exists() else []:
        s = json.loads(f.read_text())
        live[pd.Timestamp(s["day"])] = {"pred": s["pred"], "source": "live"}
    if live:
        out = pd.concat([out[~out.index.isin(list(live))], pd.DataFrame(live).T]).sort_index()
    out["pred"] = pd.to_numeric(out.pred, errors="coerce")
    return out


# ───────────────────────────── the trade (exit E2) ─────────────────────────────
def e2_legs(m1_day: pd.DataFrame, dirn: int, stop: float = STOP, book: float = BOOK) -> dict:
    """Index path from 09:45 (1-minute bars of one day, 09:45 → 15:15): stop `stop` pts against; half booked at
    +`book`, the rest's stop moves to entry; square off at the 15:15 bar's open. Stop first when both fall in one
    minute. Returns entry, legs [(fraction, time, pts, why)], pts (weighted, before costs), done (False while open)."""
    m = m1_day
    if m.empty:
        return {"legs": [], "done": False}
    e = float(m.open.iloc[0])
    sq = m.index[0].normalize() + pd.Timedelta(minutes=915)
    legs, half_done = [], None
    for t, b in m.iterrows():
        if t >= sq:
            px = float(b.open)
            if half_done is None:
                legs = [(1.0, t, (px - e) * dirn, "15:15")]
            else:
                legs.append((0.5, t, (px - e) * dirn, "15:15"))
            return {"entry": e, "legs": legs, "pts": sum(f * p for f, _, p, _ in legs), "done": True}
        hi, lo, op = float(b.high), float(b.low), float(b.open)
        adv = (e - lo) if dirn == 1 else (hi - e)
        fav = (hi - e) if dirn == 1 else (e - lo)
        gap = (e - op) * dirn                                  # how far the bar OPENED against us
        if half_done is None:
            if adv >= stop:
                loss = -max(stop, gap)                         # a gap through the stop fills at the open
                legs = [(1.0, t, loss, "stop")]
                return {"entry": e, "legs": legs, "pts": loss, "done": True}
            if fav >= book:
                half_done = t
                legs = [(0.5, t, book, "+35")]
        elif t > half_done and adv >= 0:
            legs.append((0.5, t, -max(0.0, gap), "breakeven"))
            return {"entry": e, "legs": legs, "pts": sum(f * p for f, _, p, _ in legs), "done": True}
    return {"entry": e, "legs": legs, "pts": sum(f * p for f, _, p, _ in legs) if legs else None, "done": False,
            "last": float(m.close.iloc[-1])}


# ───────────────────────────── live: 09:45 decision ─────────────────────────────
def vix_live(now: dt.datetime) -> dict:
    """VIX the way the training data has it: the 09:15 one-minute candle's open and yesterday's daily close (Angel
    candles); Angel's quote as a fallback (its 'open' is the official open, which often equals yesterday's close)."""
    out = {}
    d0 = now.replace(hour=9, minute=15, second=0, microsecond=0)
    try:
        c1 = sources.Angel.candles("INDIA VIX", d0, d0 + dt.timedelta(minutes=2), "ONE_MINUTE")
        if len(c1) and c1.index[0] == pd.Timestamp(d0):
            out["vix_open"] = float(c1.open.iloc[0])
        cd = sources.Angel.candles("INDIA VIX", d0 - dt.timedelta(days=10), now, "ONE_DAY")
        if len(cd):
            cd = cd[pd.DatetimeIndex(cd.index).normalize() < pd.Timestamp(now.date())]
        if len(cd):
            out["vix_prev"] = float(cd.close.iloc[-1])
    except Exception as e:
        log.warning("vix candles: %s", e)
    if "vix_open" not in out or "vix_prev" not in out:
        sources.Angel.token(SYM)
        vt = (sources.Angel._tokens or {}).get(("NSE", "INDIA VIX"))
        q = sources.Angel.quotes({"NSE": [vt]}, "FULL").get(str(vt)) or {} if vt else {}
        if q.get("open") and q.get("close"):
            out.setdefault("vix_open", float(q["open"]))
            out.setdefault("vix_prev", float(q["close"]))
    return out


def decide_today(now: dt.datetime | None = None, save: bool = True) -> dict:
    """Build today's 09:45 row from live data, predict, save data/edge/signals/<day>.json."""
    from .sync import now_ist, latest_minutes
    now = now or now_ist()
    day = pd.Timestamp(now.date())
    p = DIR / "signals" / f"{day.date()}.json"
    if p.exists() and save:
        return json.loads(p.read_text())
    M = load_model()
    if M is None:
        return {"error": "no model yet — run: docker exec chartlab python -m app.edge train"}
    prev = [x for x in _trading_days_back(6) if x < now.date()]
    if prev:
        status["nse"] = update_nse(prev[-3:])                       # yesterday's OI / participant files if missing
    d = latest_minutes(SYM)                                         # today's 1m bars (Angel)
    if d is not None and len(d):
        store.save_ext(SYM, d, "1")
    live = vix_live(now)
    b = breadth_snapshot(day.date(), save=save)
    if b:
        live["breadth"] = b
    ctx = Context()
    f = ctx.row(day, live)
    if f is None:
        return {"error": "today's 09:15–09:45 bars are incomplete"}
    x = pd.DataFrame([{k: f.get(k, np.nan) for k in M["meta"]["features"]}])
    p_, used = predict(M, x)
    pred = float(p_[0])
    sig = 1 if pred >= THRESH else -1 if pred <= -THRESH else 0
    missing = [k for k in M["meta"]["features"] if not np.isfinite(float(x[k].iloc[0]))]
    out = {"day": str(day.date()), "pred": pred, "signal": sig, "strong": abs(pred) >= STRONG,
           "adr": f["_adr"], "pred_pts": pred * f["_adr"], "missing": missing, "model_used": str(used[0]),
           "features": {k: (None if not np.isfinite(float(x[k].iloc[0])) else float(x[k].iloc[0])) for k in M["meta"]["features"]},
           "model": M["meta"]["trained_until"], "at": now.isoformat(timespec="seconds")}
    if save:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=1))
    log.info("morning edge %s: pred %.3f → %s (missing %s)", day.date(), pred, sig, missing)
    return out


def _trading_days_back(n: int) -> list:
    d = store.bars(SYM, "D")
    return [t.date() for t in d.index[-n:]]


def loop():
    """Background job: keep the NSE EOD files current (evenings / mornings) and make the 09:45 decision."""
    from .sync import now_ist
    last_nse = 0.0
    while True:
        try:
            t = now_ist()
            m = t.hour * 60 + t.minute
            if config.OFFLINE:
                return
            if time.time() - last_nse > 3 * 3600 and (m < 9 * 60 + 10 or m > 19 * 60):
                status["state"] = "updating NSE files"
                days = [d for d in _trading_days_back(8) if d < t.date() or m > 19 * 60]
                status["nse"] = update_nse(days)
                last_nse = time.time()
                status["state"] = "idle"
            # the decision needs 09:45 breadth: only between 09:45 and 09:52 (a late start skips the day, honestly)
            if t.weekday() < 5 and 9 * 60 + 45 <= m <= 9 * 60 + 52 and sources.Angel.available():
                if not (DIR / "signals" / f"{t.date()}.json").exists():
                    status["state"] = "deciding"
                    status["today"] = decide_today(t)
                    if "error" in status["today"]:
                        log.warning("morning edge %s: %s", t.date(), status["today"]["error"])
                    status["state"] = "idle"
        except Exception as e:
            log.exception("edge loop")
            status["error"] = str(e)[:200]
        time.sleep(20)


def start():
    if config.OFFLINE:
        return
    DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=loop, daemon=True, name="edge").start()


if __name__ == "__main__":                       # docker exec chartlab python -m app.edge train | dataset
    import sys
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "train"
    if cmd == "nse":
        print(update_nse(_trading_days_back(int(sys.argv[2]) if len(sys.argv) > 2 else 15)))
    elif cmd == "dataset":
        ds = dataset(); ds.to_parquet(DIR / "dataset.parquet"); print(ds.shape)
    elif cmd == "today":
        print(json.dumps(decide_today(), indent=1)[:3000])
    elif cmd == "dry":                                   # pipeline check without saving anything
        r = decide_today(save=False)
        print(json.dumps({k: v for k, v in r.items() if k != "features"}, indent=1), "\n", r.get("features"))
    else:
        print(train())
