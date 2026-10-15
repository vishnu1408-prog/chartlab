"""
Copied from /home/kniti/indicators/tests/harmonic_ref.py (+ `created`, `run.open`, `run.pending` for chartlab).
Reference implementation of the logic in harmonic_patterns.pine.
Mirrors the Pine code 1:1 (ratio specs, PRZ, score, zigzag, setup state machine)
so the logic can be unit-tested outside TradingView.
"""
from dataclasses import dataclass, field

NAMES = ["Gartley", "Bat", "Alt Bat", "Butterfly", "Crab", "Deep Crab", "Shark", "Cypher", "AB=CD"]
CYPHER = 7
ABCD = 8
# per pattern: xabLo, xabHi, r2Lo, r2Hi, bcdLo, bcdHi, dLo, dHi
#   r2 = BC/AB (Cypher: XC/XA);  d = AD/XA (Cypher: CD/XC, AB=CD: CD/AB, X ignored)
SPEC = [
    [0.618, 0.618, 0.382, 0.886, 1.272, 1.618, 0.786, 0.786],  # Gartley
    [0.382, 0.500, 0.382, 0.886, 1.618, 2.618, 0.886, 0.886],  # Bat
    [0.236, 0.382, 0.382, 0.886, 2.000, 3.618, 1.130, 1.130],  # Alt Bat
    [0.786, 0.786, 0.382, 0.886, 1.618, 2.618, 1.272, 1.414],  # Butterfly
    [0.382, 0.618, 0.382, 0.886, 2.240, 3.618, 1.618, 1.618],  # Crab
    [0.886, 0.886, 0.382, 0.886, 2.000, 3.618, 1.618, 1.618],  # Deep Crab
    [0.382, 0.886, 1.130, 1.618, 1.618, 2.240, 0.886, 1.130],  # Shark
    [0.382, 0.618, 1.272, 1.414, 0.000, 100.0, 0.786, 0.786],  # Cypher
    [0.000, 100.0, 0.382, 0.886, 1.130, 2.618, 1.000, 1.272],  # AB=CD
]
NPAT = len(SPEC)


def d_leg(pid, x, a, b, c):
    """(base, leg) so that D = base - ratio * leg for the primary D ratio"""
    if pid == CYPHER:
        return c, c - x
    if pid == ABCD:
        return c, a - b
    return a, a - x


def in_range(r, lo, hi, tol):
    return lo * (1 - tol) <= r <= hi * (1 + tol)


def prz(pid, x, a, b, c, tol, requireBC=True):
    """Return (ok, top, bot) of the Potential Reversal Zone for point D.
    requireBC=False: zone = primary D level only (BC projection not required to overlap)."""
    s = SPEC[pid]
    xa, ab, bc = abs(a - x), abs(b - a), abs(c - b)
    ok = xa > 0 and ab > 0 and bc > 0
    xab = ab / xa if ok else 0.0
    r2 = (abs(c - x) / xa if pid == CYPHER else bc / ab) if ok else 0.0
    ok = ok and in_range(xab, s[0], s[1], tol) and in_range(r2, s[2], s[3], tol)
    base, leg = d_leg(pid, x, a, b, c)
    d1 = base - s[6] * (1 - tol) * leg
    d2 = base - s[7] * (1 + tol) * leg
    e1 = c - s[4] * (1 - tol) * (c - b)
    e2 = c - s[5] * (1 + tol) * (c - b)
    if requireBC or pid == ABCD:
        lo = max(min(d1, d2), min(e1, e2))
        hi = min(max(d1, d2), max(e1, e2))
    else:
        lo, hi = min(d1, d2), max(d1, d2)
    ok = ok and lo <= hi
    return ok, hi, lo


def dev(r, lo, hi):
    return (lo - r) / lo if r < lo and lo > 0 else (r - hi) / hi if r > hi else 0.0


def score(pid, x, a, b, c, d, tol):
    s = SPEC[pid]
    xa = abs(a - x)
    xab = abs(b - a) / xa
    r2 = abs(c - x) / xa if pid == CYPHER else abs(c - b) / abs(b - a)
    base, leg = d_leg(pid, x, a, b, c)
    dr = abs(d - base) / abs(leg)
    tot = 0.2 * dev(xab, s[0], s[1]) + 0.2 * dev(r2, s[2], s[3]) + 0.6 * dev(dr, s[6], s[7])
    return round(100 * max(0.0, 1 - tot / max(tol, 0.01)))


# ---------------------------------------------------------------- bar engine
@dataclass
class Setup:
    pid: int; dir: int
    x: float; a: float; b: float; c: float
    xBar: int; aBar: int; bBar: int; cBar: int
    top: float; bot: float
    state: int = 0          # 0 waiting, 1 in PRZ, 2 trade open, 3 done, 9 dead
    d: float = 0.0; dBar: int = 0; rsiX: float = 50.0
    entry: float = 0.0; sl: float = 0.0; t1: float = 0.0; t2: float = 0.0
    entryBar: int = 0; t1Hit: bool = False; score: int = 0
    result: str = ""; r: float = 0.0; risk: float = 0.0
    reason: str = ""; mfe: float = 0.0; mae: float = 0.0; exitBar: int = 0
    feat: dict = None
    created: int = 0                 # bar at which the setup became known (chartlab: replay visibility)


@dataclass
class Params:
    # v2 defaults = harmonic_patterns.pine defaults (chosen on NIFTY 2015-21, validated 2022+ and BANK)
    pivLen: int = 3; minSwingAtr: float = 0.0; tol: float = 0.08
    confMode: str = "Reversal candle"; rsiOS: float = 35; rsiOB: float = 65
    confWin: int = 10; maxLegBars: int = 100; invAtr: float = 0.25; minScore: int = 0
    slAtr: float = 0.25; t1Fib: float = 0.618; t2Fib: float = 1.0
    beAfterT1: bool = True; maxTradeBars: int = 100
    enabled: tuple = (True,) * 9  # Gartley … Cypher, AB=CD
    requireBC: bool = False       # True: PRZ = XA level ∩ BC projection (strict); False: XA level only
    # real-data filters (0 / None = off)
    maxPrzAtr: float = 0.0        # PRZ must be narrower than this x ATR (tight Fibonacci confluence)
    minCdBars: int = 0            # C->D leg must last at least this many bars
    entryWindow: tuple = None     # (startMin, endMin) minutes-of-day when new entries are allowed
    scanDepth: int = 2            # 1 = last 4 pivots only; >1 also tries older pivots as B/A/X (multi-degree)
    onePerBar: bool = True        # at most one signal per bar and direction (highest score wins)
    oneActive: bool = False       # no new signal in a direction while a trade in that direction is open
    # ---- Pine v3 intraday mode (needs ctx= in run())
    intraday: bool = False        # entry window, square-off, pacing exactly like harmonic_patterns.pine v3
    entryWin: tuple = (570, 870)  # 09:30-14:30 (minutes of day)
    sqOffMin: int = 915           # 15:15 square-off
    oneAtATime: bool = True
    # ---- context filters (None = off)
    stretchMin: float = None      # dir * (dayOpen - close) / ADR >= this
    stretchMax: float = None      # dir * (dayOpen - close) / ADR <= this  (don't fade a strong trend day)
    levelAtr: float = None        # D within this x ATR of PDH / PDL / PDC / day open
    pinbar: bool = False          # D bar must reject: wick >= 50% of its range on the reversal side
    noEntryWin: tuple = None      # (startMin, endMin) with no new entries, e.g. lunch (690, 810)
    trailAtr: float = None        # after T1: trail the stop this many ATR behind the best high/low
    noT2: bool = False            # with trailing: no fixed T2, runner exits on trail / square-off
    minRiskAtr: float = None      # widen the stop so it is at least this many ATR from entry
    maxPerDay: int = 0            # cap on signals per trading day (0 = off; needs day= in run())
    minGapBars: int = 0           # min bars between two signals (any direction)


def pivots(vals, L, high):
    """Pine-like ta.pivothigh/pivotlow: value at i confirmed at i+L."""
    out = [None] * len(vals)
    for i in range(L, len(vals) - L):
        w = vals[i - L:i + L + 1]
        v = vals[i]
        others = w[:L] + w[L + 1:]
        if (high and all(v > o for o in others)) or (not high and all(v < o for o in others)):
            out[i + L] = v
    return out


def atr_series(o, h, l, c, n=14):
    tr, out, prev = [], [], None
    for i in range(len(c)):
        t = h[i] - l[i] if i == 0 else max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        tr.append(t)
        prev = t if prev is None else (prev * (n - 1) + t) / n  # RMA
        out.append(prev)
    return out


def rsi_series(c, n=14):
    out, ag, al = [50.0] * len(c), None, None
    for i in range(1, len(c)):
        ch = c[i] - c[i - 1]
        g, lo = max(ch, 0.0), max(-ch, 0.0)
        ag = g if ag is None else (ag * (n - 1) + g) / n
        al = lo if al is None else (al * (n - 1) + lo) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def segment_ok(P, i, j):
    """pivots strictly between i and j stay inside [P[i], P[j]] -> i..j is a valid higher-degree leg"""
    lo, hi = min(P[i], P[j]), max(P[i], P[j])
    return all(lo <= P[k] <= hi for k in range(i + 1, j))


def xabc_combos(P, depth):
    """Index tuples (X, A, B, C) with C = last pivot. depth=1 -> only the last four pivots."""
    n = len(P)
    out = []
    for j in range(n - 2, n - 2 - 2 * depth, -2):                 # B
        for k in range(j - 1, j - 1 - 2 * depth, -2):             # A
            for m in range(k - 1, k - 1 - 2 * depth, -2):         # X
                if m < 0:
                    continue
                if segment_ok(P, m, k) and segment_ok(P, k, j) and segment_ok(P, j, n - 1):
                    out.append((m, k, j, n - 1))
    return out


def context_features(s, i, o, h, l, c, atr, ctx):
    """intraday context at the signal bar"""
    d = s.dir
    stretch = d * (ctx["dayOpen"][i] - c[i]) / max(ctx["adr"][i], 1e-9)
    lv = [v for v in (ctx["pdh"][i], ctx["pdl"][i], ctx["pdc"][i], ctx["dayOpen"][i]) if v == v]
    lvl = min(abs(s.d - v) for v in lv) / max(atr[i], 1e-9) if lv else 99.0
    k = s.dBar
    rng = max(h[k] - l[k], 1e-9)
    wick = (min(o[k], c[k]) - l[k]) if d == 1 else (h[k] - max(o[k], c[k]))
    return dict(stretch=stretch, lvl=lvl, pin=wick / rng >= 0.5, wickp=wick / rng, tod=ctx["tod"][i],
                gap=d * (ctx["pdc"][i] - ctx["dayOpen"][i]) / max(ctx["adr"][i], 1e-9) if ctx["pdc"][i] == ctx["pdc"][i] else 0.0)


def run(o, h, l, c, p: Params, rsi=None, tod=None, day=None, ctx=None):
    n = len(c)
    rsi = rsi or rsi_series(c)
    atr = atr_series(o, h, l, c)
    ph, pl = pivots(h, p.pivLen, True), pivots(l, p.pivLen, False)
    zzP, zzB, zzD = [], [], []
    setups, signals, trades, dead = [], [], [], []
    dayCount, lastSig = 0, -10**9
    if ctx is not None:
        tod, day = ctx['tod'], ctx['day']

    def add_pivot(price, bidx, d, minMove):
        if not zzP:
            zzP.append(price); zzB.append(bidx); zzD.append(d); return 1
        if zzD[-1] == d:
            if (d == 1 and price > zzP[-1]) or (d == -1 and price < zzP[-1]):
                zzP[-1] = price; zzB[-1] = bidx; return 2
            return 0
        if abs(price - zzP[-1]) >= minMove:
            zzP.append(price); zzB.append(bidx); zzD.append(d); return 1
        return 0

    for i in range(n):
        minMove = p.minSwingAtr * atr[i]
        ev = 0
        first_low = bool(zzD) and zzD[-1] == 1
        order = [(pl[i], -1, l), (ph[i], 1, h)] if first_low else [(ph[i], 1, h), (pl[i], -1, l)]
        for val, d, _ in order:
            if val is not None:
                e = add_pivot(val, i - p.pivLen, d, minMove)
                ev = e if e else ev
        combos = xabc_combos(zzP, p.scanDepth) if ev and len(zzP) >= 4 else []
        for (im, ik, ij, ic) in combos:
            x, a, b, cc = zzP[im], zzP[ik], zzP[ij], zzP[ic]
            xB, aB, bB, cB = zzB[im], zzB[ik], zzB[ij], zzB[ic]
            dr = 1 if a > x else -1
            for pid in range(NPAT):
                if not p.enabled[pid] or (pid == ABCD and im != ik - 1):   # AB=CD ignores X: build once
                    continue
                ok, top, bot = prz(pid, x, a, b, cc, p.tol, p.requireBC)
                if not ok:
                    continue
                ext, extBar = cc, cB
                for k in range(cB + 1, i + 1):
                    if (dr == 1 and l[k] < ext) or (dr == -1 and h[k] > ext):
                        ext, extBar = (l[k] if dr == 1 else h[k]), k
                far = bot - p.invAtr * atr[i] if dr == 1 else top + p.invAtr * atr[i]
                if (dr == 1 and ext < far) or (dr == -1 and ext > far):
                    continue
                setups.append(Setup(pid, dr, x, a, b, cc, xB, aB, bB, cB, top, bot, d=ext, dBar=extBar,
                                    rsiX=50.0))
                setups[-1].created = i
        cands = []
        for s in list(setups):
            bull = s.dir == 1
            if s.state in (0, 1):
                if (bull and h[i] > s.c) or (not bull and l[i] < s.c):
                    s.reason = "C broken (after PRZ)" if s.state == 1 else "C broken (never reached PRZ)"
                    s.state = 9
                else:
                    if (bull and l[i] < s.d) or (not bull and h[i] > s.d):
                        s.d, s.dBar = (l[i] if bull else h[i]), i
                    far = s.bot - p.invAtr * atr[i] if bull else s.top + p.invAtr * atr[i]
                    beyond = s.d < far if bull else s.d > far
                    inZone = s.d <= s.top if bull else s.d >= s.bot
                    if beyond or i - s.cBar > p.maxLegBars:
                        s.state = 9; s.reason = "overshoot PRZ" if beyond else "leg too long"
                    elif inZone:
                        s.state = 1
                        s.rsiX = min(s.rsiX, rsi[i]) if bull else max(s.rsiX, rsi[i])
                        fresh = i - s.dBar <= p.confWin
                        rev = (c[i] > o[i] and c[i] > h[i - 1]) if bull else (c[i] < o[i] and c[i] < l[i - 1])
                        if p.confMode == "Touch PRZ":
                            trig = True
                        elif p.confMode == "Reversal candle":
                            trig = rev
                        elif p.confMode == "Reversal close":      # softer: close in reversal direction, above prior close
                            trig = (c[i] > o[i] and c[i] > c[i - 1]) if bull else (c[i] < o[i] and c[i] < c[i - 1])
                        elif p.confMode.startswith("Candle:"):     # candlestick pattern trigger (candles.py)
                            from candles import candle
                            trig = candle(p.confMode[7:], o, h, l, c, i, bull)
                        else:
                            trig = rev and (s.rsiX <= p.rsiOS if bull else s.rsiX >= p.rsiOB)
                        s.score = score(s.pid, s.x, s.a, s.b, s.c, s.d, p.tol)
                        t1 = s.d + p.t1Fib * (s.a - s.d)
                        notPast = c[i] < t1 if bull else c[i] > t1
                        filt = (p.maxPrzAtr <= 0 or s.top - s.bot <= p.maxPrzAtr * atr[i]) \
                            and s.dBar - s.cBar >= p.minCdBars \
                            and (p.entryWindow is None or tod is None or p.entryWindow[0] <= tod[i] < p.entryWindow[1])
                        if ctx is not None:
                            s.feat = context_features(s, i, o, h, l, c, atr, ctx)
                            filt = filt and (p.stretchMin is None or s.feat['stretch'] >= p.stretchMin) and (p.stretchMax is None or s.feat['stretch'] <= p.stretchMax) \
                                and (p.levelAtr is None or s.feat['lvl'] <= p.levelAtr) and (not p.pinbar or s.feat['pin'])
                        sl = (min(s.d, s.bot) - p.slAtr * atr[i]) if bull else (max(s.d, s.top) + p.slAtr * atr[i])
                        if not fresh:
                            s.state = 9; s.reason = "no confirmation"
                        elif trig and notPast and filt and s.score >= p.minScore and abs(c[i] - sl) > 0:
                            cands.append((s, sl, t1))
            elif s.state == 2 and i > s.entryBar and p.intraday and day[i] != day[i - 1]:
                R = lambda px: (px - s.entry) / s.risk * s.dir      # missed square-off: exit at today's open
                s.r = (0.5 * R(s.t1) + 0.5 * R(o[i])) if s.t1Hit else R(o[i])
                s.result = "EOD"; s.state = 3; s.exitBar = i; trades.append(s)
            elif s.state == 2 and i > s.entryBar:
                R = lambda px: (px - s.entry) / s.risk * s.dir
                s.mfe = max(s.mfe, R(h[i] if bull else l[i]))
                s.mae = min(s.mae, R(l[i] if bull else h[i]))
                hitSL = l[i] <= s.sl if bull else h[i] >= s.sl
                hitT1 = h[i] >= s.t1 if bull else l[i] <= s.t1
                hitT2 = h[i] >= s.t2 if bull else l[i] <= s.t2
                if hitSL:
                    s.r = (0.5 * R(s.t1) + 0.5 * R(s.sl)) if s.t1Hit else -1.0
                    s.result = "BE" if s.t1Hit else "SL"
                    s.state = 3
                else:
                    if hitT1 and not s.t1Hit:
                        s.t1Hit = True
                        if p.beAfterT1:
                            s.sl = s.entry
                    if hitT2:
                        s.r = 0.5 * R(s.t1) + 0.5 * R(s.t2); s.result = "T2"; s.state = 3
                    elif i - s.entryBar >= p.maxTradeBars or (p.intraday and tod[i] >= p.sqOffMin):
                        s.r = (0.5 * R(s.t1) + 0.5 * R(c[i])) if s.t1Hit else R(c[i])
                        s.result = "EOD" if p.intraday and tod[i] >= p.sqOffMin else "TO"; s.state = 3
                if s.state == 2 and s.t1Hit and p.trailAtr:          # trail for the next bar (no look-ahead)
                    s.sl = max(s.sl, h[i] - p.trailAtr * atr[i]) if bull else min(s.sl, l[i] + p.trailAtr * atr[i])
                if s.state == 3:
                    s.exitBar = i
                    trades.append(s)
            if s.state == 9:
                dead.append((s.pid, s.reason))
            if s.state in (3, 9):
                setups.remove(s)
        # trigger: all candidates, or only the best-scoring one per direction
        if day is not None and i > 0 and day[i] != day[i - 1]:
            dayCount = 0
        if p.intraday:
            nOpen = sum(1 for t in setups if t.state == 2)
            inWin = p.entryWin[0] <= tod[i] < p.entryWin[1] and tod[i] < p.sqOffMin \
                and not (p.noEntryWin and p.noEntryWin[0] <= tod[i] < p.noEntryWin[1])
            can = inWin and dayCount < p.maxPerDay and i - lastSig >= p.minGapBars and not (p.oneAtATime and nOpen > 0)
            if can and cands:
                s, sl, t1 = max(cands, key=lambda v: v[0].score)      # first max, like Pine's strict '>'
                dayCount += 1; lastSig = i
                if p.minRiskAtr and abs(c[i] - sl) < p.minRiskAtr * atr[i]:
                    sl = c[i] - s.dir * p.minRiskAtr * atr[i]
                s.state = 2; s.entry, s.entryBar = c[i], i; s.sl = sl; s.risk = abs(s.entry - s.sl)
                s.t1 = t1; s.t2 = s.d + p.t2Fib * (s.a - s.d)
                if p.noT2:
                    s.t2 = s.entry + s.dir * 1e9
                signals.append((i, s.pid, s.dir))
                for q, _, _ in cands:
                    if q.state != 2:
                        dead.append((q.pid, "same move")); setups.remove(q)
            continue
        busy = {t.dir for t in setups if t.state == 2} if p.oneActive else set()
        best = {}
        for s, sl, t1 in cands:
            if s.dir in busy:
                s.state = -1
                continue
            if not p.onePerBar or s.dir not in best or s.score > best[s.dir][0].score:
                if p.onePerBar and s.dir in best:
                    best[s.dir][0].state = -1
                best[s.dir if p.onePerBar else id(s)] = (s, sl, t1)
            else:
                s.state = -1
        for s, sl, t1 in sorted(best.values(), key=lambda v: -v[0].score):
            if (p.maxPerDay and day is not None and dayCount >= p.maxPerDay) or i - lastSig < p.minGapBars:
                s.state = -1
                continue
            dayCount += 1
            lastSig = i if p.minGapBars else lastSig
            s.state = 2
            s.entry, s.entryBar = c[i], i
            s.sl = sl
            s.risk = abs(s.entry - s.sl)       # initial risk (1R), fixed
            s.t1 = t1
            s.t2 = s.d + p.t2Fib * (s.a - s.d)
            signals.append((i, s.pid, s.dir))
        for s, _, _ in cands:
            if s.state == -1:                  # lost to a better pattern on the same bar
                dead.append((s.pid, "duplicate")); setups.remove(s)
    run.dead = dead
    run.open = [t for t in setups if t.state == 2]          # chartlab: trade still open at the last bar
    run.pending = [t for t in setups if t.state in (0, 1)]  # chartlab: forming patterns (PRZ not yet resolved)
    return signals, trades, (zzP, zzB)
