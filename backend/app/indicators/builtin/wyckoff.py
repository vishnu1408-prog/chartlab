"""Wyckoff [theUltimator5] — port of the TradingView Pine v6 indicator (/home/kniti/indicators/sample.pine) for STUDY.

Ported: the chart-timeframe campaign engine `f_engine` (Pine lines ~1060–1765) with the hard-coded constants of lines
51–176 — bar measurements (ATR14, effort = volume, or bar spread when the symbol has no volume, ATR-adaptive pivots),
PS / PSY, SC / BC (score + absorption), AR, ST, Phase B (tests, opposite-edge tests, traversals, departures), Phase C
(Spring / UTAD / terminal test + Test), Phase D (SOS / SOW, LPS / LPSY, demotion), Phase E, every reset rule, and the
entry policy of the three strictness levels (lines 1729–1747) plus the "Buy/Sell Label Trigger" choices.
Skipped: the higher-timeframe override (request.security with lookahead_on — it peeks at unfinished HTF bars on
history), reference-schematic panels / overlays, the draggable schematic, status / diagnostic tables.

Replay rules: the engine runs bar by bar on closed bars only. Pine re-draws its labels from the LIVE campaign state on
the last bar (so retracted events disappear and moved ones jump); here every event is drawn once, at its own bar, with
visible_from = the bar on which the engine recorded it (pivot events are known `pivot length` bars late), so nothing
repaints. The range box is drawn piece-wise: each piece starts when the range top / bottom / colour last changed.

Entries are NOT a validated strategy — the per-chart tally (next-bar open, fixed target / stop, 15:15 square-off,
0.02 % cost) is only there so you can see what the signals would have done on the loaded bars.
"""
import math
from bisect import bisect_left, bisect_right

import numpy as np
import pandas as pd

from ... import store, symbols
from ..base import Indicator, Output, Param, times

NA = float("nan")
COST = 0.0002
MINTICK = 0.05

# ── hard-coded settings (Pine lines 51–176) ──────────────────────────────────────────────────────────────────────
pivotLen, adaptiveSwingMin, adaptiveSwingMax, adaptiveSwingVolLen = 4, 2, 10, 50
trendLen, priorTrendMaxBars, priorTrendPivotFactor = 25, 200, 8.0
priorTrendStrong, priorTrendNeutral, continuationTrendLen, oppositeCycleMinBars, extremeLen = 35.0, 15.0, 200, 25, 30
phaseAMinBars, phaseAAfterArMaxBars, phaseBIdleMaxBars, phaseCIdleMaxBars = 6, 45, 125, 50
phaseAPrematureDepartureBars, phaseAPrematureDepartureTRFrac, phaseAPrematureDepartureATR = 3, 0.30, 0.75
structureInvalidationATR, structureInvalidationBars = 0.50, 2
phaseBDepartureBars, phaseBDepartureTRFrac, phaseBDepartureATR, phaseDFailBars, phaseEMaxBars = 3, 0.35, 0.75, 3, 300
volLen, atrLen, climaxVolMult, climaxSpreadMult, relaxedClimaxFactor = 50, 14, 1.8, 1.5, 0.80
prelimVolMult, prelimExtremeATR, prelimEfficiencyLookback, prelimEfficiencyRatio, prelimMinScore = 1.25, 0.75, 10, 0.80, 4
scCloseMin, bcCloseMax, climaxMinScore = 0.35, 0.65, 5
climaxAbsorptionMinBars, climaxAbsorptionMaxBars = 3, 8
climaxAbsorptionExtremeATR, climaxAbsorptionReboundATR, climaxAbsorptionVolFloor = 0.50, 0.35, 0.90
minARATR, maxARBars, arMaxExtensionATR, boundaryTolATR = 2.0, 30, 15.0, 0.75
stMaxVolRatio, stMaxSpreadRatio, stMinScore = 0.90, 0.90, 4
minPhaseBBars, minPhaseBTests, minPhaseBOppositeTests, minPhaseBTraversals = 30, 2, 1, 2
phaseBZoneFrac, phaseBRangeMinATR, phaseBRangeMaxATR, phaseBBadTestCooldownFactor, phaseBEdgeExpansionCap = 0.25, 1.5, 8.0, 2.0, 0.15
terminalTestMinBarsAfterST, terminalTestVolRatio, terminalTestSpreadRatio, terminalTestOtherMaxRatio = 3, 0.85, 0.85, 1.05
excursionRecoveryBars, springMinPenATR, springMaxPenATR, springCloseMin, utadCloseMax = 3, 0.15, 2.5, 0.55, 0.45
springEffortMaxMult, excursionMinScore, testTolATR, testMaxVolRatio, testMaxSpreadRatio = 1.50, 3, 1.25, 0.80, 0.80
testExtremeToleranceATR, testMinScore = 0.15, 2
phaseCToDMinBars, phaseDMinBars, phaseDValidationMin, phaseDDominanceFrac, breakATR = 3, 4, 60, 0.65, 0.15
strengthVolMult, strengthSpreadATR, sosCloseMin, sowCloseMax, strengthMinScore = 1.15, 1.15, 0.65, 0.35, 3
sosMultiBarLen, sosMultiBarATR, sosMultiBarEffort = 5, 2.0, 1.10
lpsBoundaryATR, lpsVolMult, lpsSpreadATR, lpsMinScore = 1.50, 1.00, 1.00, 2
confirmBars, directAcceptanceBars, directAcceptanceConfidence, minConfidencePhaseC, minConfidencePhaseD = 3, 5, 75, 45, 55
ENTRY_CONF = {"Conservative": 75, "Standard": 60, "Aggressive": 55}
ENTRY_TESTS = {"Conservative": 7, "Standard": 6, "Aggressive": 5}

ACC, DST = 1, -1
P_NONE, P_A, P_B, P_C, P_D, P_E = 0, 1, 2, 3, 4, 5
PHASES = {"A": P_A, "B": P_B, "C": P_C, "D": P_D, "E": P_E}
LETTER = {P_NONE: "-", P_A: "A", P_B: "B", P_C: "C", P_D: "D", P_E: "E"}
RS = ["invalidated", "no absorption", "no range", "left range", "stale", "demoted", "phase E ended"]
TRIGGERS = ["Auto (Entry Strictness)", "Phase C Test", "SOS / SOW", "LPS / LPSY", "Phase E"]
ENTRY_NAME = {"test": "Phase C Test", "strength": "SOS/SOW", "lps": "LPS/LPSY", "e": "Phase E"}


# ── Pine helpers (na = NaN; math.max / min of na is na) ─────────────────────────────────────────────────────────
def na(x):
    return x != x


def nz(x, d=0.0):
    return d if x != x else x


def pmax(a, b):
    return NA if (a != a or b != b) else (a if a > b else b)


def pmin(a, b):
    return NA if (a != a or b != b) else (a if a < b else b)


def pround(x):
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def pine_atr(h, l, c, n=14):
    """ta.atr: RMA of the true range, seeded with the SMA of the first n values (NaN before)."""
    pc = np.r_[np.nan, c[:-1]]
    tr = np.where(np.isnan(pc), h - l, np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc))))
    out = np.full(len(tr), np.nan)
    if len(tr) >= n:
        v = tr[:n].mean()
        out[n - 1] = v
        for k in range(n, len(tr)):
            v = (v * (n - 1) + tr[k]) / n
            out[k] = v
    return out


def _roll(x, n, how):
    s = pd.Series(x)
    r = s.rolling(n, min_periods=n)
    return getattr(r, how)().to_numpy()


def _shift(x, k=1, fill=np.nan):
    out = np.empty_like(x, dtype=float)
    out[:k] = fill
    out[k:] = x[:-k]
    return out


def pivots(src, p, high=True):
    """ta.pivothigh / ta.pivotlow(src, p, p) evaluated on every bar (value = src[i - p] or NaN). Ties: the pivot must
    beat the p bars to its left strictly and be at least equal to the p bars to its right (assumed Pine semantics)."""
    n = len(src)
    out = np.full(n, np.nan)
    if n < 2 * p + 1:
        return out
    w = np.lib.stride_tricks.sliding_window_view(src, 2 * p + 1)          # window k covers bars k .. k + 2p
    c = w[:, p]
    left, right = w[:, :p], w[:, p + 1:]
    if high:
        ok = (c > left.max(axis=1)) & (c >= right.max(axis=1))
    else:
        ok = (c < left.min(axis=1)) & (c <= right.min(axis=1))
    out[2 * p:] = np.where(ok, c, np.nan)
    return out


def measure(df: pd.DataFrame, mintick=MINTICK) -> dict:
    """Vectorised bar measurements of f_engine (Pine lines 1063–1159). Pure."""
    O, H, L, C = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    V = np.nan_to_num(df["volume"].to_numpy(float)) if "volume" in df else np.zeros(len(df))
    noVol = np.cumsum(V) <= 0.0
    spr = np.maximum(H - L, mintick)
    eff = np.where(noVol, spr, V)
    avgEffRaw = _roll(eff, volLen, "mean")
    avgEff = np.where(np.isnan(avgEffRaw), eff, avgEffRaw)
    avgEffPrev = np.where(np.isnan(_shift(avgEffRaw)), avgEff, _shift(avgEffRaw))
    atrRaw = pine_atr(H, L, C, atrLen)
    a = np.where(np.isnan(atrRaw), spr, atrRaw)
    aPrev = np.where(np.isnan(_shift(atrRaw)), a, _shift(atrRaw))
    unit = np.maximum(a, mintick)
    cPos = (C - L) / spr
    relE = eff / np.maximum(avgEff, 1e-10)
    sprATR = spr / unit
    priorLo = _shift(_roll(L, extremeLen, "min"))
    priorHi = _shift(_roll(H, extremeLen, "max"))
    with np.errstate(invalid="ignore"):
        aBase = _roll(a, adaptiveSwingVolLen, "mean")
        aRatio = np.where(np.nan_to_num(aBase) > 0, a / np.where(np.nan_to_num(aBase) > 0, aBase, 1.0), 1.0)
        liveLen = np.clip(np.floor(pivotLen * aRatio + 0.5), adaptiveSwingMin, adaptiveSwingMax).astype(int)
        preLen = np.r_[liveLen[:3], liveLen[:-3]] if len(liveLen) > 3 else liveLen.copy()
        longSma = _roll(C, continuationTrendLen, "mean")
        c1 = _shift(C)
        close1 = np.where(np.isnan(c1), C, c1)
        close2 = np.where(np.isnan(_shift(C, 2)), C, _shift(C, 2))
        ls1, ls20 = _shift(longSma), _shift(longSma, 20)
        longBull = (np.nan_to_num(c1) > np.nan_to_num(ls1)) & (np.nan_to_num(ls1) > np.nan_to_num(ls20))
        longBear = ~np.isnan(ls20) & (np.nan_to_num(c1) < np.nan_to_num(ls1)) & (np.nan_to_num(ls1) < np.nan_to_num(ls20))
        effort2 = np.maximum(relE, 0.01) * np.maximum(sprATR, 0.01)
        dnEffc = np.maximum(close1 - C, 0.0) / unit / effort2
        upEffc = np.maximum(C - close1, 0.0) / unit / effort2
        dnBase = _shift(_roll(dnEffc, prelimEfficiencyLookback, "mean"))
        upBase = _shift(_roll(upEffc, prelimEfficiencyLookback, "mean"))
        mbLow, mbHigh = _roll(L, sosMultiBarLen, "min"), _roll(H, sosMultiBarLen, "max")
        mbEffRatio = _roll(eff, sosMultiBarLen, "sum") / np.maximum(avgEff * sosMultiBarLen, 1e-10)
        c3 = np.where(np.isnan(_shift(C, 3)), C, _shift(C, 3))
        m = dict(
            O=O, H=H, L=L, C=C, eff=eff, spr=spr, avgEffRaw=avgEffRaw, avgEff=avgEff, avgEffPrev=avgEffPrev,
            atrRaw=atrRaw, a=a, aPrev=aPrev, unit=unit, cPos=cPos, relE=relE, sprATR=sprATR,
            recentEff=_roll(eff, climaxAbsorptionMinBars, "mean"), recentLow=_roll(L, climaxAbsorptionMinBars, "min"),
            recentHigh=_roll(H, climaxAbsorptionMinBars, "max"), priorLo=priorLo, priorHi=priorHi,
            effHiPrior=_shift(_roll(eff, extremeLen, "max")), newLow=L <= priorLo, newHigh=H >= priorHi,
            liveLen=liveLen, preLen=preLen, longSma=longSma, longBull=longBull, longBear=longBear,
            dnAbsorb=dnEffc <= dnBase * prelimEfficiencyRatio, upAbsorb=upEffc <= upBase * prelimEfficiencyRatio,
            nearRecentLow=L <= priorLo + a * prelimExtremeATR, nearRecentHigh=H >= priorHi - a * prelimExtremeATR,
            hiCInv=_roll(C, structureInvalidationBars, "max"), loCInv=_roll(C, structureInvalidationBars, "min"),
            hiCDep=_roll(C, phaseBDepartureBars, "max"), loCDep=_roll(C, phaseBDepartureBars, "min"),
            hiCA=_roll(C, phaseAPrematureDepartureBars, "max"), loCA=_roll(C, phaseAPrematureDepartureBars, "min"),
            hiCConf=_roll(C, confirmBars, "max"), loCConf=_roll(C, confirmBars, "min"),
            hiCDir=_roll(C, directAcceptanceBars, "max"), loCDir=_roll(C, directAcceptanceBars, "min"),
            hiCFail=_roll(C, phaseDFailBars, "max"), loCFail=_roll(C, phaseDFailBars, "min"),
            mbSOS=((C - mbLow) / unit >= sosMultiBarATR) & (mbEffRatio >= sosMultiBarEffort) & (cPos >= 0.50),
            mbSOW=((mbHigh - C) / unit >= sosMultiBarATR) & (mbEffRatio >= sosMultiBarEffort) & (cPos <= 0.50),
            res3Up=np.maximum(C - c3, 0.0) / unit, res3Dn=np.maximum(c3 - C, 0.0) / unit,
            sosScore=((relE >= strengthVolMult).astype(int) + (sprATR >= strengthSpreadATR) + (cPos >= sosCloseMin)
                      + ((C > O) & (C - O >= a * 0.50))),
            sowScore=((relE >= strengthVolMult).astype(int) + (sprATR >= strengthSpreadATR) + (cPos <= sowCloseMax)
                      + ((C < O) & (O - C >= a * 0.50))),
            close1=close1, close2=close2)
    # prior-trend score parts that do not need the (state-dependent) pivot series — f_priorTrendScore
    n = len(C)
    idx = np.arange(n)
    nb = np.maximum(trendLen, np.minimum(priorTrendMaxBars, np.floor(liveLen * priorTrendPivotFactor + 0.5))).astype(int)
    ne = np.maximum(2, np.minimum(8, np.floor(liveLen * 0.75 + 0.5))).astype(int)
    iE, iS = idx - ne, idx - ne - nb
    ok = iS >= 0
    iEc, iSc = np.clip(iE, 0, None), np.clip(iS, 0, None)
    unitA = np.maximum(a[iEc], mintick)
    net = C[iEc] - C[iSc]
    disp = np.clip(net / (unitA * np.sqrt(nb)), -3.0, 3.0) * 15.0
    ad = np.r_[0.0, np.abs(np.diff(C))]
    cs = np.cumsum(ad)
    path = cs[iEc] - cs[iSc]
    with np.errstate(invalid="ignore", divide="ignore"):
        er = np.where(path > 0, np.abs(net) / np.where(path > 0, path, 1.0), 0.0)
    erPart = np.where(net >= 0, 1.0, -1.0) * er * 25.0
    sE, sS = longSma[iEc], longSma[iSc]
    smaPart = np.where(np.isnan(sE) | np.isnan(sS), 0.0, np.where(sE > sS, 10.0, np.where(sE < sS, -10.0, 0.0)))
    m.update(tr_ok=ok, tr_lo=iS + 1, tr_hi=iE, tr_base=disp + erPart + smaPart)
    m["PH"] = {p: pivots(H, p, True) for p in range(adaptiveSwingMin, adaptiveSwingMax + 1)}
    m["PL"] = {p: pivots(L, p, False) for p in range(adaptiveSwingMin, adaptiveSwingMax + 1)}
    return m


class _Swings:
    """Running pivot series for the swing part of f_priorTrendScore (count of HH/LH or HL/LL inside a bar window)."""

    def __init__(self):
        self.bars, self.vals, self.up, self.dn = [], [], [0], [0]

    def add(self, bar, v):
        if self.vals:
            self.up.append(self.up[-1] + (v > self.vals[-1]))
            self.dn.append(self.dn[-1] + (v < self.vals[-1]))
        self.bars.append(bar)
        self.vals.append(v)

    def count(self, lo, hi):
        ja, jb = bisect_left(self.bars, lo), bisect_right(self.bars, hi) - 1
        if jb <= ja:
            return 0, 0
        return self.up[jb] - self.up[ja], self.dn[jb] - self.dn[ja]


class WS:
    """Campaign state (Pine `type WS`); na fields are NaN."""
    _DEF = dict(stopSide=0, outcome=0, phase=0, ev=0, startBar=NA, pivLen=NA, birthTrend=NA, ctxBull=False, ctxBear=False,
                climaxBar=NA, climaxTime=NA, climaxPrice=NA, climaxEff=NA, climaxSpr=NA, climaxATR=NA, climaxScore=0,
                absorbed=False, psTime=NA, psPrice=NA, psScore=0, origHigh=NA, origLow=NA, rangeHigh=NA, rangeLow=NA,
                arRunPrice=NA, arRunBar=NA, arRunTime=NA, arOK=False, arBar=NA, arTime=NA, arPrice=NA, stCount=0, stN=0,
                stEffSum=0.0, stSprSum=0.0, lastGoodEff=NA, lastGoodSpr=NA, stBar=NA, stTime=NA, stPrice=NA, stScore=0,
                edge1=NA, edge2=NA, edge3=NA, oppCount=0, opN=0, opEffSum=0.0, opSprSum=0.0, opLastBar=NA,
                opLastPrice=NA, travCount=0, lastZone=0, lastZoneBar=NA, lastBadBar=NA, bStartBar=NA, bStartTime=NA,
                cStartTime=NA, cReadyBar=NA, dStartBar=NA, dStartTime=NA, eStartBar=NA, eStartTime=NA, pend=False,
                pendEdge=0, pendStartBar=NA, pendExtBar=NA, pendExt=NA, pendExtTime=NA, pendEff=NA, pendSpr=NA,
                pendCoolBar=NA, provEdge=0, provBar=NA, provPrice=NA, provTime=NA, provEff=NA, provSpr=NA, provScore=0,
                exc=False, excTested=False, excBar=NA, excPrice=NA, excTime=NA, excEff=NA, excSpr=NA, excScore=0,
                testTime=NA, testPrice=NA, testScore=0, strBar=NA, strTime=NA, strPrice=NA, strScore=0, lpsTime=NA,
                lpsPrice=NA, lpsBar=NA, lpsScore=0, entryTime=NA, entryPrice=NA, entryKind="", outCount=0,
                lastEventBar=NA, lastEventTime=NA, lastEventPrice=NA, lastActBar=NA, cfPrior=False, cfClimax=False,
                cfAR=False, cfST=False, cfExc=False, cfTest=False, cfStrength=False, cfLPS=False, cfAccept=False, cid=-1)

    def __init__(self):
        self.__dict__.update(self._DEF)


# ── pure scoring helpers (Pine lines 615–700) ───────────────────────────────────────────────────────────────────
def structure_confidence(prior, climax, ar, st, cdone, strength, lps, accept):
    return 10 * prior + 15 * climax + 10 * ar + 15 * st + 15 * cdone + 15 * strength + 10 * lps + 10 * accept


def validation_score(trend, absorbed, rangeATR, st, opp, trav, clean, cause, terminal, strength, lps, accept):
    v = 10 if abs(trend) >= priorTrendStrong else 5 if abs(trend) >= priorTrendNeutral else 0
    v += 15 if absorbed else 0
    if phaseBRangeMinATR <= rangeATR <= phaseBRangeMaxATR:
        v += 10
    elif phaseBRangeMinATR * 0.75 <= rangeATR <= phaseBRangeMaxATR * 1.5:
        v += 5
    v += 15 if st >= minPhaseBTests else 8 if st >= 1 else 0
    v += 15 if (opp >= 1 and trav >= 2) else 8 if (opp >= 1 or trav >= 1) else 0
    v += 10 if clean else 0
    v += 10 if cause >= 3.0 else 5 if cause >= 1.5 else 0
    v += 10 if terminal else 0
    v += 3 if strength else 0
    v += 2 if lps else 0
    return min(v, 100)


def cause_units(trav, bAge, p, rangeATR):
    return bAge / max(p * 6.0, 1.0) + trav * 0.5 + (0.5 if rangeATR >= phaseBRangeMinATR else 0.0)


def test_class(near, holds, closeOK, e, sp, cE, cS):
    if not near:
        return "none"
    if not holds:
        return "failed"
    return "good" if (closeOK and e <= cE * stMaxVolRatio and sp <= cS * stMaxSpreadRatio) else "poor"


def phase_a_min_bars(p):
    return max(phaseAMinBars, max(p, 1) * 2)


def phase_b_min_bars(p, rangeATR):
    p = max(p, 1)
    raw = pround(max(float(p * 6), rangeATR * p))
    return max(minPhaseBBars, min(raw, minPhaseBBars * 2))


def phase_c_to_d_min_bars(p):
    return max(phaseCToDMinBars, max(p, 1) // 2)


def phase_d_min_bars(p):
    return max(phaseDMinBars, max(p, 1))


def exc_recovery_limit(p):
    return max(excursionRecoveryBars, pround(max(p, 1) * 0.75))


def phase_idle_limit(phase, hasAR, p):
    p = max(p, 1)
    if phase == P_A:
        return max(phaseAAfterArMaxBars, p * 6) if hasAR else max(maxARBars, p * 4)
    if phase == P_B:
        return max(phaseBIdleMaxBars, p * 20)
    if phase == P_C:
        return max(phaseCIdleMaxBars, p * 8)
    if phase == P_D:
        return max(phaseCIdleMaxBars * 2, p * 16)
    return None


def character_improving(n, effSum, sprSum, lastE, lastS):
    if n >= 2 and not na(lastE) and not na(lastS):
        return lastE <= effSum / n and lastS <= sprSum / n * 1.05
    return False


def robust_edge(e1, e2, e3, orig, is_low):
    med = NA
    if not na(e1) and not na(e2) and not na(e3):
        med = e1 + e2 + e3 - max(e1, e2, e3) - min(e1, e2, e3)
    elif not na(e1) and not na(e2):
        med = ((e1 + e2) / 2.0 + orig) / 2.0
    elif not na(e1):
        med = (e1 + orig) / 2.0
    if na(med):
        return orig
    return pmax(orig, med) if is_low else pmin(orig, med)


def type_name(stop, out, bull, bear):
    if out == ACC:
        return "Reaccumulation" if (stop == DST or bull) else "Accumulation"
    if out == DST:
        return "Redistribution" if (stop == ACC or bear) else "Distribution"
    return "-"


# ── engine ──────────────────────────────────────────────────────────────────────────────────────────────────────
def run_engine(df: pd.DataFrame, strictness="Standard", mintick=MINTICK) -> dict:
    """Bar-by-bar port of f_engine on closed bars. Pure. Returns
      campaigns : [{cid, stop, events:[{kind,text,bar,price,known}], phase_bar:{phase: first bar}, max_phase, start, end}]
      entries   : [{mode, dir, bar (event bar), known (qualification bar), price, kind}]   (mode auto/test/strength/lps/e)
      state     : per-bar arrays cid / phase / rh / rl / col (0 blue, 1 green, -1 red)
      last      : final WS, resets : counts per reason."""
    m = measure(df, mintick)
    n = len(df)
    T = times(df).astype(float)
    step = np.diff(T)
    tfs = float(np.median(step[step > 0])) if (step > 0).any() else 60.0
    a_np = m["a"]
    # plain Python scalars in the loop: faster indexing, and bool + bool sums are ints (numpy bools would OR)
    m = {k: ({p: v.tolist() for p, v in x.items()} if isinstance(x, dict) else x.tolist()) for k, x in m.items()}
    T = T.tolist()
    O, H, L, C = m["O"], m["H"], m["L"], m["C"]
    eff, spr, a_, atrRaw, avgEff, avgEffRaw = m["eff"], m["spr"], m["a"], m["atrRaw"], m["avgEff"], m["avgEffRaw"]
    liveLenA, cPosA, relEA, sprATRA = m["liveLen"], m["cPos"], m["relE"], m["sprATR"]
    PH, PL = m["PH"], m["PL"]
    swH, swL = _Swings(), _Swings()
    eMinConf, eMinTests = ENTRY_CONF[strictness], ENTRY_TESTS[strictness]
    eValFloor = phaseDValidationMin if strictness == "Conservative" else max(45, phaseDValidationMin - 10)

    camps, entries, seen = [], [], set()
    st_cid, st_phase = np.full(n, -1), np.zeros(n, dtype=int)
    st_rh, st_rl, st_col = np.full(n, np.nan), np.full(n, np.nan), np.zeros(n, dtype=int)
    rs_counts = [0] * 7

    s = WS()
    regime, regimeBar = 0, NA
    prelimBar = prelimTime = prelimPrice = NA
    prelimDir, prelimScore = 0, 0
    lastPLBar = lastPHBar = NA
    i = 0
    close = high = low = a = tm = NA

    def log(kind, text, bar, price):
        if s.cid >= 0:
            camps[s.cid]["events"].append({"kind": kind, "text": text, "bar": int(bar), "price": float(price), "known": i,
                                           "dir": s.outcome or s.stopSide})

    def set_event(ev):
        s.ev, s.lastEventBar, s.lastEventTime, s.lastEventPrice, s.lastActBar = ev, i, tm, close, i

    def bias_dir():
        if s.stopSide == ACC:
            return DST if s.ctxBear else ACC
        if s.stopSide == DST:
            return ACC if s.ctxBull else DST
        return 0

    def range_atr(atrNow):
        if na(s.rangeHigh) or na(s.rangeLow):
            return 0.0
        return abs(s.rangeHigh - s.rangeLow) / max(nz(s.climaxATR, atrNow), mintick)

    def hard_level(side):
        if side == ACC:
            if s.exc and s.outcome == ACC and not na(s.excPrice):
                return s.excPrice
            return s.rangeLow if na(s.origLow) else pmin(s.origLow, nz(s.rangeLow, s.origLow))
        if s.exc and s.outcome == DST and not na(s.excPrice):
            return s.excPrice
        return s.rangeHigh if na(s.origHigh) else pmax(s.origHigh, nz(s.rangeHigh, s.origHigh))

    def conf():
        cdone = s.cfTest or (s.cfExc and s.cfStrength)
        return structure_confidence(s.cfPrior, s.cfClimax, s.cfAR, s.cfST, cdone, s.cfStrength, s.cfLPS, s.cfAccept)

    def validation(p, trendNow, atrNow):
        if s.stopSide == 0:
            return 0
        rATR = range_atr(atrNow)
        bAge = max(i - s.bStartBar, 0) if not na(s.bStartBar) else 0
        badAge = -1 if na(s.lastBadBar) else i - s.lastBadBar
        clean = badAge < 0 or badAge >= pround(p * phaseBBadTestCooldownFactor)
        cause = cause_units(s.travCount, bAge, p, rATR)
        terminal = (s.cfTest and (not s.exc or s.excTested)) or (s.cfExc and s.cfStrength)
        return validation_score(nz(s.birthTrend, trendNow), s.absorbed, rATR, s.stCount, s.oppCount, s.travCount, clean,
                                cause, terminal, s.cfStrength, s.cfLPS, s.cfAccept)

    def mature(p, atrNow):
        rATR = range_atr(atrNow)
        bAge = i - s.bStartBar if not na(s.bStartBar) else 0
        bMin = phase_b_min_bars(p, rATR)
        badAge = -1 if na(s.lastBadBar) else i - s.lastBadBar
        clean = badAge < 0 or badAge >= pround(p * phaseBBadTestCooldownFactor)
        opp = minPhaseBOppositeTests <= 0 or s.oppCount >= minPhaseBOppositeTests
        trav = minPhaseBTraversals <= 0 or s.travCount >= minPhaseBTraversals
        ch = character_improving(s.stN, s.stEffSum, s.stSprSum, s.lastGoodEff, s.lastGoodSpr)
        support = opp + trav + clean + ch
        sane = phaseBRangeMinATR * 0.75 <= rATR <= phaseBRangeMaxATR * 2.0
        return s.phase == P_B and bAge >= bMin and s.stCount >= minPhaseBTests and sane and support >= 2

    def readiness(minConf, valFloor, atrNow):
        bull = s.outcome == ACC
        if na(s.rangeHigh) or na(s.rangeLow):
            distATR = 99.0
        else:
            distATR = ((close - s.rangeHigh) if bull else (s.rangeLow - close)) / max(atrNow, mintick)
        ageBars = (tm - s.lastEventTime) / max(tfs, 1.0) if not na(s.lastEventTime) else 999.0
        terminal = (s.exc and s.excTested) or (not s.exc and s.cfTest)
        r = (s.outcome != 0) + (s.phase >= P_C) + terminal + s.cfStrength + s.cfLPS + (conf() >= minConf)
        r += validation(int(nz(s.pivLen, pivotLen)), s.birthTrend, atrNow) >= valFloor
        r += distATR <= 2.0
        r += ageBars <= 50.0
        return r

    def freeze_ar():
        s.arBar, s.arTime, s.arPrice, s.cfAR = s.arRunBar, s.arRunTime, s.arRunPrice, True
        if s.stopSide == ACC:
            s.origHigh = s.rangeHigh = s.arRunPrice
        else:
            s.origLow = s.rangeLow = s.arRunPrice
        log("AR", "AR", s.arBar, s.arPrice)

    def register_st(price, pb, e, sp, score):
        s.stCount += 1
        s.stN += 1
        s.stEffSum += e
        s.stSprSum += sp
        s.lastGoodEff, s.lastGoodSpr = e, sp
        s.stBar, s.stTime, s.stPrice, s.stScore = pb, T[pb], price, score
        s.edge3, s.edge2, s.edge1 = s.edge2, s.edge1, price
        if s.stopSide == ACC:
            s.rangeLow = robust_edge(s.edge1, s.edge2, s.edge3, s.origLow, True)
        else:
            s.rangeHigh = robust_edge(s.edge1, s.edge2, s.edge3, s.origHigh, False)
        s.cfST = True
        set_event("ST")
        log("ST", f"ST {score}/6", pb, price)

    def register_opp(price, pb, e, sp):
        s.oppCount += 1
        s.opN += 1
        s.opEffSum += e
        s.opSprSum += sp
        s.opLastBar, s.opLastPrice = pb, price
        s.lastActBar = i

    def clear_terminal():
        s.exc = s.excTested = False
        s.excBar = s.excPrice = s.excTime = s.excEff = s.excSpr = NA
        s.excScore = 0
        s.testTime = s.testPrice = NA
        s.testScore = 0
        s.cfExc = s.cfTest = False
        s.cReadyBar = s.cStartTime = NA

    def exc_from_pend(score):
        s.exc, s.excTested = True, False
        s.excBar, s.excPrice, s.excTime, s.excEff, s.excSpr = s.pendExtBar, s.pendExt, s.pendExtTime, s.pendEff, s.pendSpr
        s.excScore, s.cfExc, s.outcome, s.cStartTime, s.phase = score, True, s.pendEdge, s.pendExtTime, P_C
        s.cReadyBar, s.pend, s.provEdge = NA, False, 0
        set_event("SPRING" if s.outcome == ACC else "UTAD")
        log("EXC", f"{'Spring' if s.outcome == ACC else 'UTAD'} {score}/4", s.excBar, s.excPrice)

    def adopt_prov():
        s.exc, s.excTested = True, False
        s.excBar, s.excPrice, s.excTime, s.excEff, s.excSpr = s.provBar, s.provPrice, s.provTime, s.provEff, s.provSpr
        s.excScore, s.cfExc, s.outcome, s.cStartTime, s.provEdge = s.provScore, True, s.provEdge, s.provTime, 0
        log("EXC", f"{'Spring' if s.outcome == ACC else 'UTAD'} {s.excScore}/4", s.excBar, s.excPrice)

    def enter_ctest(outc, pb, price, score):
        s.phase, s.outcome, s.cStartTime, s.cReadyBar = P_C, outc, T[pb], i
        s.testTime, s.testPrice, s.testScore, s.cfTest, s.pend = T[pb], price, score, True, False
        set_event("CTEST")
        log("TEST", f"C-Test {score}/3", pb, price)

    def promote_d(outc, score, price):
        s.outcome, s.phase, s.dStartBar, s.dStartTime = outc, P_D, i, tm
        s.strBar, s.strTime, s.strPrice, s.strScore = i, tm, price, score
        s.cfStrength, s.pend, s.outCount = True, False, 0
        set_event("SOS" if outc == ACC else "SOW")
        log("STR", f"{'SOS' if outc == ACC else 'SOW'} {score}/4", i, price)

    def demote_b():
        clear_terminal()
        s.outcome, s.phase = 0, P_B
        s.strBar = s.strTime = s.strPrice = NA
        s.strScore = 0
        s.lpsTime = s.lpsPrice = s.lpsBar = NA
        s.lpsScore = 0
        s.dStartBar = s.dStartTime = NA
        s.cfStrength = s.cfLPS = s.pend = False
        s.provEdge, s.outCount = 0, 0
        s.lastBadBar = s.lastActBar = i

    for i in range(n):
        tm, close, high, low, open_ = T[i], C[i], H[i], L[i], O[i]
        a = a_[i]
        e_i, spr_i, cPos, relE, sprATR = eff[i], spr[i], cPosA[i], relEA[i], sprATRA[i]
        avgE = avgEff[i]
        liveLen = int(liveLenA[i])
        pLen = int(nz(s.pivLen, liveLen)) if s.phase != P_NONE else liveLen
        ph, pl = PH[pLen][i], PL[pLen][i]
        pivBar = i - pLen
        if pivBar >= 0:
            j = pivBar
            pA = max(nz(atrRaw[j], a), mintick)
            pEff, pSpr = eff[j], max(H[j] - L[j], mintick)
            pCPos = (C[j] - L[j]) / pSpr
            pAvgEff = nz(avgEffRaw[j], avgE)
        else:
            pA, pEff, pSpr = max(a, mintick), e_i, max(high - low, mintick)
            pCPos, pAvgEff = (close - low) / pSpr, avgE
        # f_priorTrendScore (swing part uses the pivot series as the engine produced it)
        if m["tr_ok"][i]:
            hh, lh = swH.count(m["tr_lo"][i], m["tr_hi"][i])
            hl, ll = swL.count(m["tr_lo"][i], m["tr_hi"][i])
            sw = hh + ll + lh + hl
            trendScore = clamp(m["tr_base"][i] + ((hh + hl - lh - ll) / sw * 20.0 if sw > 0 else 0.0), -100.0, 100.0)
        else:
            trendScore = 0.0
        if not na(ph):
            swH.add(i, ph)
        if not na(pl):
            swL.add(i, pl)
        downTrend, upTrend = trendScore <= -priorTrendNeutral, trendScore >= priorTrendNeutral
        strongTrend = abs(trendScore) >= priorTrendStrong
        sosScore, sowScore = int(m["sosScore"][i]), int(m["sowScore"][i])
        mbSOS, mbSOW = bool(m["mbSOS"][i]), bool(m["mbSOW"][i])
        hiCInv, loCInv, hiCFail, loCFail = m["hiCInv"][i], m["loCInv"][i], m["hiCFail"][i], m["loCFail"][i]
        close1, close2 = m["close1"][i], m["close2"][i]
        ctxBars = max(priorTrendMaxBars, continuationTrendLen) * 2
        sig = set()

        p0 = int(nz(s.pivLen, liveLen))
        rk0 = not na(s.rangeHigh) and not na(s.rangeLow)
        rH0, rL0 = s.rangeHigh, s.rangeLow
        rHt0 = max(rH0 - rL0, mintick) if rk0 else NA
        rMid0 = (rH0 + rL0) / 2.0 if rk0 else NA
        probeOpen = s.pend and i - s.pendStartBar <= exc_recovery_limit(p0)
        structAge = i - s.startBar if not na(s.startBar) else 0
        ageOKBull = regime != DST or structAge >= oppositeCycleMinBars
        ageOKBear = regime != ACC or structAge >= oppositeCycleMinBars
        hardBull0, hardBear0 = hard_level(ACC), hard_level(DST)
        resetWhy = -1

        # Retire structures that no longer fit
        if s.phase == P_A:
            age = i - s.climaxBar
            invA = s.stopSide == ACC and hiCInv < s.climaxPrice - a * structureInvalidationATR
            invD = s.stopSide == DST and loCInv > s.climaxPrice + a * structureInvalidationATR
            absExp = not s.absorbed and age > climaxAbsorptionMaxBars
            noAR = not s.arOK and age > maxARBars
            overExt = s.arOK and not na(s.arRunPrice) and abs(s.arRunPrice - s.climaxPrice) / max(nz(s.climaxATR, a), mintick) > arMaxExtensionATR
            aBuf = max(rHt0 * phaseAPrematureDepartureTRFrac, a * phaseAPrematureDepartureATR) if rk0 else NA
            depart = age > maxARBars and rk0 and ((s.stopSide == ACC and m["loCA"][i] > rH0 + aBuf) or
                                                  (s.stopSide == DST and m["hiCA"][i] < rL0 - aBuf))
            if invA or invD:
                resetWhy = 0
            elif absExp:
                resetWhy = 1
            elif noAR or overExt or depart:
                resetWhy = 2
        if s.phase == P_C and not probeOpen and resetWhy < 0:
            if (s.outcome == ACC and not na(hardBull0) and hiCInv < hardBull0 - a * structureInvalidationATR) or \
               (s.outcome == DST and not na(hardBear0) and loCInv > hardBear0 + a * structureInvalidationATR):
                resetWhy = 0
        if s.phase == P_D and rk0 and resetWhy < 0:
            if s.outcome == ACC:
                if not na(hardBull0) and hiCInv < hardBull0 - a * structureInvalidationATR:
                    resetWhy = 0
                elif hiCFail < rMid0:
                    demote_b()
                    rs_counts[5] += 1
                    log("DEM", "demoted → B", i, close)
            elif s.outcome == DST:
                if not na(hardBear0) and loCInv > hardBear0 + a * structureInvalidationATR:
                    resetWhy = 0
                elif loCFail > rMid0:
                    demote_b()
                    rs_counts[5] += 1
                    log("DEM", "demoted → B", i, close)
        if s.phase == P_E and rk0 and resetWhy < 0:
            eFail = hiCFail < rMid0 if s.outcome == ACC else loCFail > rMid0
            eOld = phaseEMaxBars > 0 and not na(s.eStartBar) and i - s.eStartBar > phaseEMaxBars
            if eFail or eOld:
                resetWhy = 6
        idleLim = phase_idle_limit(s.phase, s.arOK, p0)
        if resetWhy < 0 and not probeOpen and idleLim is not None and not na(s.lastActBar) and i - s.lastActBar > idleLim:
            resetWhy = 4
        if resetWhy >= 0:
            rs_counts[resetWhy] += 1
            if s.cid >= 0:
                camps[s.cid]["why"] = RS[resetWhy]
            s = WS()

        # Track preliminary support / supply and seed new climaxes
        if s.phase in (P_NONE, P_E):
            psSc = downTrend + bool(m["nearRecentLow"][i]) + (relE >= prelimVolMult) + (sprATR >= 0.80) + (cPos >= 0.45) + bool(m["dnAbsorb"][i])
            psySc = upTrend + bool(m["nearRecentHigh"][i]) + (relE >= prelimVolMult) + (sprATR >= 0.80) + (cPos <= 0.55) + bool(m["upAbsorb"][i])
            psStale = na(prelimBar) or i - prelimBar > extremeLen
            if downTrend and m["nearRecentLow"][i] and relE >= prelimVolMult and psSc >= prelimMinScore and \
                    (prelimDir != ACC or psStale or psSc >= prelimScore):
                prelimBar, prelimTime, prelimPrice, prelimDir, prelimScore = i, tm, low, ACC, psSc
            elif upTrend and m["nearRecentHigh"][i] and relE >= prelimVolMult and psySc >= prelimMinScore and \
                    (prelimDir != DST or psStale or psySc >= prelimScore):
                prelimBar, prelimTime, prelimPrice, prelimDir, prelimScore = i, tm, high, DST, psySc

        relaxK = relaxedClimaxFactor if strongTrend else 1.0
        volHit = e_i >= m["avgEffPrev"][i] * climaxVolMult * relaxK
        sprHit = spr_i >= m["aPrev"][i] * climaxSpreadMult * relaxK
        effHiPrior = m["effHiPrior"][i]
        exceptional = (not na(effHiPrior) and e_i >= effHiPrior) or spr_i >= m["aPrev"][i] * climaxSpreadMult * 1.5
        newLow, newHigh = bool(m["newLow"][i]), bool(m["newHigh"][i])
        scSc = downTrend + newLow + volHit + sprHit + (cPos >= scCloseMin) + exceptional
        bcSc = upTrend + newHigh + volHit + sprHit + (cPos <= bcCloseMax) + exceptional
        seedOpen = s.phase in (P_NONE, P_E)
        canSC = seedOpen or (s.phase == P_A and s.stopSide == ACC and low < s.climaxPrice)
        canBC = seedOpen or (s.phase == P_A and s.stopSide == DST and high > s.climaxPrice)
        isSC = canSC and downTrend and newLow and volHit and sprHit and scSc >= climaxMinScore
        isBC = canBC and upTrend and newHigh and volHit and sprHit and bcSc >= climaxMinScore
        if isSC != isBC:
            side = ACC if isSC else DST
            recentMarkup = regime == ACC and not na(regimeBar) and i - regimeBar <= ctxBars
            recentMarkdown = regime == DST and not na(regimeBar) and i - regimeBar <= ctxBars
            if s.cid >= 0 and s.phase == P_A:
                camps[s.cid]["why"] = "re-seeded"
            elif s.cid >= 0:
                camps[s.cid]["why"] = "new climax"
            s = WS()
            s.cid = len(camps)
            camps.append({"cid": s.cid, "stop": side, "events": [], "phase_bar": {P_A: i}, "start": i, "why": ""})
            sc = scSc if isSC else bcSc
            s.stopSide, s.phase, s.startBar, s.pivLen, s.birthTrend = side, P_A, i, int(m["preLen"][i]), trendScore
            s.ctxBull, s.ctxBear = bool(recentMarkup or m["longBull"][i]), bool(recentMarkdown or m["longBear"][i])
            s.climaxBar, s.climaxTime, s.climaxPrice = i, tm, low if side == ACC else high
            s.climaxEff, s.climaxSpr, s.climaxATR, s.climaxScore = e_i, spr_i, a, sc
            if side == ACC:
                s.origLow = s.rangeLow = low
            else:
                s.origHigh = s.rangeHigh = high
            s.cfPrior = True
            set_event("SC" if side == ACC else "BC")
            log("CL", f"{'SC' if side == ACC else 'BC'} {sc}/6", i, s.climaxPrice)
            if prelimDir == side and not na(prelimBar) and prelimBar < i and i - prelimBar <= extremeLen:
                s.psTime, s.psPrice, s.psScore = prelimTime, prelimPrice, prelimScore
                log("PS", "PS" if side == ACC else "PSY", prelimBar, prelimPrice)

        # Confirm stopping action and build the AR
        if s.phase == P_A and not s.absorbed and not na(s.climaxBar):
            absAge = i - s.climaxBar
            if climaxAbsorptionMinBars <= absAge <= climaxAbsorptionMaxBars:
                effortOK = m["recentEff"][i] >= avgE * climaxAbsorptionVolFloor
                accAbs = s.stopSide == ACC and m["recentLow"][i] >= s.climaxPrice - s.climaxATR * climaxAbsorptionExtremeATR \
                    and close >= s.climaxPrice + s.climaxATR * climaxAbsorptionReboundATR
                dstAbs = s.stopSide == DST and m["recentHigh"][i] <= s.climaxPrice + s.climaxATR * climaxAbsorptionExtremeATR \
                    and close <= s.climaxPrice - s.climaxATR * climaxAbsorptionReboundATR
                if effortOK and (accAbs or dstAbs):
                    s.absorbed = s.cfClimax = True
                    s.lastActBar = i
        if s.phase == P_A and na(s.arBar) and not na(s.climaxBar) and i > s.climaxBar and i - s.climaxBar <= maxARBars:
            if s.stopSide == ACC:
                if na(s.arRunPrice) or high > s.arRunPrice:
                    s.arRunPrice, s.arRunBar, s.arRunTime = high, i, tm
                s.arOK = s.arOK or s.arRunPrice - s.climaxPrice >= a * minARATR
            else:
                if na(s.arRunPrice) or low < s.arRunPrice:
                    s.arRunPrice, s.arRunBar, s.arRunTime = low, i, tm
                s.arOK = s.arOK or s.climaxPrice - s.arRunPrice >= a * minARATR
        if s.phase == P_A and s.arOK and s.absorbed and na(s.arBar):
            if s.stopSide == ACC:
                s.rangeHigh = s.origHigh = s.arRunPrice
            else:
                s.rangeLow = s.origLow = s.arRunPrice

        p1 = int(nz(s.pivLen, liveLen))
        rk1 = not na(s.rangeHigh) and not na(s.rangeLow)
        rH1, rL1 = s.rangeHigh, s.rangeLow
        origHt1 = max(nz(s.origHigh, rH1) - nz(s.origLow, rL1), mintick) if rk1 else NA
        mature1 = mature(p1, a)
        newPL = not na(pl) and (na(lastPLBar) or pivBar > lastPLBar)
        newPH = not na(ph) and (na(lastPHBar) or pivBar > lastPHBar)
        if newPL:
            lastPLBar = pivBar
        if newPH:
            lastPHBar = pivBar
        plUsed = phUsed = False

        # terminal (no-Spring / no-UTAD) tests from a mature Phase B
        for lowSide in (True, False):
            if not ((newPL if lowSide else newPH) and s.phase == P_B and mature1 and rk1):
                continue
            refST = s.stopSide == (ACC if lowSide else DST)
            refN = s.stN if refST else s.opN
            refBar = s.stBar if refST else s.opLastBar
            refPrice = s.stPrice if refST else s.opLastPrice
            if refN > 0 and not na(refBar):
                refEff = (s.stEffSum if refST else s.opEffSum) / refN
                refSpr = (s.stSprSum if refST else s.opSprSum) / refN
                later = pivBar >= refBar + terminalTestMinBarsAfterST
                if lowSide:
                    near = pl <= rL1 + pA * testTolATR
                    holds = pl >= min(nz(s.origLow, rL1), rL1) - pA * testExtremeToleranceATR
                    beyond = na(refPrice) or pl >= refPrice - pA * testExtremeToleranceATR
                else:
                    near = ph >= rH1 - pA * testTolATR
                    holds = ph <= max(nz(s.origHigh, rH1), rH1) + pA * testExtremeToleranceATR
                    beyond = na(refPrice) or ph <= refPrice + pA * testExtremeToleranceATR
                qV, qS = pEff <= refEff * terminalTestVolRatio, pSpr <= refSpr * terminalTestSpreadRatio
                nV, nS = pEff <= refEff * terminalTestOtherMaxRatio, pSpr <= refSpr * terminalTestOtherMaxRatio
                sc = qV + qS + ((pCPos >= 0.50) if lowSide else (pCPos <= 0.50))
                if later and near and holds and beyond and ((qV and nS) or (qS and nV)) and sc >= testMinScore:
                    enter_ctest(ACC if lowSide else DST, pivBar, pl if lowSide else ph, int(sc))
                    sig.add("TEST")
                    if lowSide:
                        plUsed = True
                    else:
                        phUsed = True

        # Phase A: the first good ST freezes the AR and opens Phase B
        if newPL and not plUsed and s.phase == P_A and s.stopSide == ACC and s.absorbed and s.arOK and not na(s.arRunBar) and pivBar > s.arRunBar:
            zoneTop = s.origLow + (s.arRunPrice - s.origLow) * phaseBZoneFrac
            nearL = abs(pl - s.origLow) <= pA * boundaryTolATR or pl <= zoneTop
            holdsL = pl >= s.origLow - pA * boundaryTolATR
            closeL = pCPos >= 0.45
            tc = test_class(nearL, holdsL, closeL, pEff, pSpr, s.climaxEff, s.climaxSpr)
            sc = nearL + (pEff <= s.climaxEff * stMaxVolRatio) + (pSpr <= s.climaxSpr * stMaxSpreadRatio) + holdsL + closeL + 1
            if nearL:
                s.lastActBar = i
            if tc == "good" and sc >= stMinScore and pivBar - s.startBar >= phase_a_min_bars(p1) and pivBar - s.arRunBar >= max(2, p1):
                freeze_ar()
                register_st(pl, pivBar, pEff, pSpr, int(sc))
                s.phase, s.bStartBar, s.bStartTime = P_B, pivBar, T[pivBar]
                plUsed = True
        if newPH and not phUsed and s.phase == P_A and s.stopSide == DST and s.absorbed and s.arOK and not na(s.arRunBar) and pivBar > s.arRunBar:
            zoneBot = s.origHigh - (s.origHigh - s.arRunPrice) * phaseBZoneFrac
            nearH = abs(ph - s.origHigh) <= pA * boundaryTolATR or ph >= zoneBot
            holdsH = ph <= s.origHigh + pA * boundaryTolATR
            closeH = pCPos <= 0.55
            tc = test_class(nearH, holdsH, closeH, pEff, pSpr, s.climaxEff, s.climaxSpr)
            sc = nearH + (pEff <= s.climaxEff * stMaxVolRatio) + (pSpr <= s.climaxSpr * stMaxSpreadRatio) + holdsH + closeH + 1
            if nearH:
                s.lastActBar = i
            if tc == "good" and sc >= stMinScore and pivBar - s.startBar >= phase_a_min_bars(p1) and pivBar - s.arRunBar >= max(2, p1):
                freeze_ar()
                register_st(ph, pivBar, pEff, pSpr, int(sc))
                s.phase, s.bStartBar, s.bStartTime = P_B, pivBar, T[pivBar]
                phUsed = True

        # Phase B: more STs on the climax side, tests of the opposite edge
        rk1 = not na(s.rangeHigh) and not na(s.rangeLow)
        if newPL and not plUsed and s.phase == P_B and rk1 and pivBar > s.bStartBar:
            bHt = max(s.rangeHigh - s.rangeLow, mintick)
            if s.stopSide == ACC:
                bNear = abs(pl - s.rangeLow) <= pA * boundaryTolATR or abs(pl - s.origLow) <= pA * boundaryTolATR or pl <= s.rangeLow + bHt * phaseBZoneFrac
                bHolds = pl >= s.origLow - pA * boundaryTolATR
                bClose = pCPos >= 0.45
                tc = test_class(bNear, bHolds, bClose, pEff, pSpr, s.climaxEff, s.climaxSpr)
                sc = bNear + (pEff <= s.climaxEff * stMaxVolRatio) + (pSpr <= s.climaxSpr * stMaxSpreadRatio) + bHolds + bClose + 1
                isProv = s.provEdge == ACC and not na(s.provBar) and abs(pivBar - s.provBar) <= p1
                if bNear:
                    s.lastActBar = i
                if tc == "failed" and not isProv:
                    s.lastBadBar = pivBar
                elif tc == "good" and sc >= stMinScore:
                    register_st(pl, pivBar, pEff, pSpr, int(sc))
            else:
                if pl < s.rangeLow and pl >= nz(s.origLow, s.rangeLow) - origHt1 * phaseBEdgeExpansionCap:
                    s.rangeLow = pl
                oHt = max(s.rangeHigh - s.rangeLow, mintick)
                oNear = pl <= s.rangeLow + pA * boundaryTolATR or pl <= s.rangeLow + oHt * phaseBZoneFrac
                oInside = pl >= nz(s.origLow, s.rangeLow) - origHt1 * phaseBEdgeExpansionCap
                oCtrl = pEff <= s.climaxEff * 1.20 and pSpr <= s.climaxSpr * 1.20
                if oNear and oInside and oCtrl and (na(s.opLastBar) or pivBar > s.opLastBar):
                    register_opp(pl, pivBar, pEff, pSpr)
        if newPH and not phUsed and s.phase == P_B and rk1 and pivBar > s.bStartBar:
            bHt = max(s.rangeHigh - s.rangeLow, mintick)
            if s.stopSide == DST:
                hNear = abs(ph - s.rangeHigh) <= pA * boundaryTolATR or abs(ph - s.origHigh) <= pA * boundaryTolATR or ph >= s.rangeHigh - bHt * phaseBZoneFrac
                hHolds = ph <= s.origHigh + pA * boundaryTolATR
                hClose = pCPos <= 0.55
                tc = test_class(hNear, hHolds, hClose, pEff, pSpr, s.climaxEff, s.climaxSpr)
                sc = hNear + (pEff <= s.climaxEff * stMaxVolRatio) + (pSpr <= s.climaxSpr * stMaxSpreadRatio) + hHolds + hClose + 1
                isProv = s.provEdge == DST and not na(s.provBar) and abs(pivBar - s.provBar) <= p1
                if hNear:
                    s.lastActBar = i
                if tc == "failed" and not isProv:
                    s.lastBadBar = pivBar
                elif tc == "good" and sc >= stMinScore:
                    register_st(ph, pivBar, pEff, pSpr, int(sc))
            else:
                if ph > s.rangeHigh and ph <= nz(s.origHigh, s.rangeHigh) + origHt1 * phaseBEdgeExpansionCap:
                    s.rangeHigh = ph
                oHt = max(s.rangeHigh - s.rangeLow, mintick)
                oNear = ph >= s.rangeHigh - pA * boundaryTolATR or ph >= s.rangeHigh - oHt * phaseBZoneFrac
                oInside = ph <= nz(s.origHigh, s.rangeHigh) + origHt1 * phaseBEdgeExpansionCap
                oCtrl = pEff <= s.climaxEff * 1.20 and pSpr <= s.climaxSpr * 1.20
                if oNear and oInside and oCtrl and (na(s.opLastBar) or pivBar > s.opLastBar):
                    register_opp(ph, pivBar, pEff, pSpr)

        # traversals of the range
        if s.phase == P_B and not na(s.rangeHigh) and not na(s.rangeLow):
            tvHt = max(s.rangeHigh - s.rangeLow, mintick)
            loZ = newPL and pl <= s.rangeLow + tvHt * phaseBZoneFrac
            hiZ = newPH and ph >= s.rangeHigh - tvHt * phaseBZoneFrac
            zone = (-1 if loZ else 1) if loZ != hiZ else 0
            if zone != 0 and (na(s.lastZoneBar) or pivBar > s.lastZoneBar):
                if s.lastZone != 0 and zone != s.lastZone:
                    s.travCount += 1
                    s.lastActBar = i
                s.lastZone, s.lastZoneBar = zone, pivBar

        # Phase C: test of the Spring / UTAD
        for lowSide in (True, False):
            want = ACC if lowSide else DST
            if not ((newPL if lowSide else newPH) and s.phase == P_C and s.outcome == want and s.exc and not s.excTested
                    and pivBar > s.excBar and not na(s.rangeLow if lowSide else s.rangeHigh)):
                continue
            if lowSide:
                xNear, xKeep = pl <= s.rangeLow + pA * testTolATR, pl >= s.excPrice - pA * testExtremeToleranceATR
            else:
                xNear, xKeep = ph >= s.rangeHigh - pA * testTolATR, ph <= s.excPrice + pA * testExtremeToleranceATR
            sc = (pEff <= s.excEff * testMaxVolRatio) + (pSpr <= s.excSpr * testMaxSpreadRatio) + ((pCPos >= 0.50) if lowSide else (pCPos <= 0.50))
            if xNear and xKeep and sc >= testMinScore:
                price = pl if lowSide else ph
                s.excTested, s.cReadyBar = True, i
                s.testTime, s.testPrice, s.testScore, s.cfTest = T[pivBar], price, int(sc), True
                set_event("TEST")
                log("TEST", f"Test {int(sc)}/3", pivBar, price)
                sig.add("TEST")

        # Phase D: LPS / LPSY
        if newPL and s.phase == P_D and s.outcome == ACC and not na(s.strBar) and pivBar > s.strBar and not na(s.rangeHigh):
            hard = hard_level(ACC)
            side_ok = not na(hard) and pl > hard - pA * testExtremeToleranceATR
            creek = pl >= s.rangeHigh - pA * lpsBoundaryATR
            sc = (pEff <= pAvgEff * lpsVolMult) + (pSpr <= pA * lpsSpreadATR) + (pCPos >= 0.45)
            if side_ok and creek and sc >= lpsMinScore:
                s.lpsTime, s.lpsPrice, s.lpsBar, s.lpsScore, s.cfLPS = T[pivBar], pl, i, int(sc), True
                set_event("LPS")
                log("LPS", f"LPS {int(sc)}/3", pivBar, pl)
                sig.add("LPS")
        if newPH and s.phase == P_D and s.outcome == DST and not na(s.strBar) and pivBar > s.strBar and not na(s.rangeLow):
            hard = hard_level(DST)
            side_ok = not na(hard) and ph < hard + pA * testExtremeToleranceATR
            ice = ph <= s.rangeLow + pA * lpsBoundaryATR
            sc = (pEff <= pAvgEff * lpsVolMult) + (pSpr <= pA * lpsSpreadATR) + (pCPos <= 0.55)
            if side_ok and ice and sc >= lpsMinScore:
                s.lpsTime, s.lpsPrice, s.lpsBar, s.lpsScore, s.cfLPS = T[pivBar], ph, i, int(sc), True
                set_event("LPSY")
                log("LPS", f"LPSY {int(sc)}/3", pivBar, ph)
                sig.add("LPS")

        p2 = int(nz(s.pivLen, liveLen))
        rk2 = not na(s.rangeHigh) and not na(s.rangeLow)
        rH2, rL2 = s.rangeHigh, s.rangeLow
        recLimit2 = exc_recovery_limit(p2)
        mature2 = mature(p2, a)
        bAge2 = i - s.bStartBar if not na(s.bStartBar) else 0
        # Spring, UTAD probes
        probeB = s.phase == P_B and s.stCount >= 1 and bAge2 >= max(3, p2 * 2)
        probeCLo = s.phase == P_C and s.outcome == ACC and not s.exc
        probeCHi = s.phase == P_C and s.outcome == DST and not s.exc
        if rk2 and not s.pend and (na(s.pendCoolBar) or i > s.pendCoolBar):
            penLo, penHi = rL2 - low, high - rH2
            okLo = (probeB or probeCLo) and a * springMinPenATR <= penLo <= a * springMaxPenATR
            okHi = (probeB or probeCHi) and a * springMinPenATR <= penHi <= a * springMaxPenATR
            if okLo != okHi:
                s.pend, s.pendEdge, s.pendStartBar, s.pendExtBar = True, ACC if okLo else DST, i, i
                s.pendExt, s.pendExtTime, s.pendEff, s.pendSpr = low if okLo else high, tm, e_i, spr_i
        if s.pend and rk2 and s.phase in (P_B, P_C):
            if s.pendEdge == ACC and low < s.pendExt:
                s.pendExt, s.pendExtBar, s.pendExtTime = low, i, tm
                s.pendEff, s.pendSpr = max(nz(s.pendEff), e_i), max(nz(s.pendSpr), spr_i)
            elif s.pendEdge == DST and high > s.pendExt:
                s.pendExt, s.pendExtBar, s.pendExtTime = high, i, tm
                s.pendEff, s.pendSpr = max(nz(s.pendEff), e_i), max(nz(s.pendSpr), spr_i)
            recAge = i - s.pendStartBar
            recovered = close > rL2 if s.pendEdge == ACC else close < rH2
            depth = rL2 - s.pendExt if s.pendEdge == ACC else s.pendExt - rH2
            depthOK = a * springMinPenATR <= depth <= a * springMaxPenATR
            if s.pendEdge == ACC:
                recSc = (cPos >= springCloseMin) + (e_i <= avgE * springEffortMaxMult) + (close > open_) + (spr_i >= a * 0.50)
            else:
                recSc = (cPos <= utadCloseMax) + (e_i <= avgE * springEffortMaxMult) + (close < open_) + (spr_i >= a * 0.50)
            if recovered and depthOK and recAge <= recLimit2 and recSc >= excursionMinScore:
                edge = s.pendEdge
                if s.phase == P_B and not mature2:
                    s.provEdge, s.provBar, s.provPrice, s.provTime = edge, s.pendExtBar, s.pendExt, s.pendExtTime
                    s.provEff, s.provSpr, s.provScore, s.lastActBar, s.pend = s.pendEff, s.pendSpr, int(recSc), i, False
                else:
                    if s.phase == P_C:
                        clear_terminal()
                    exc_from_pend(int(recSc))
            elif recAge > recLimit2 or (not depthOK and depth > a * springMaxPenATR):
                if s.phase == P_B:
                    s.lastBadBar = i
                s.pend = False
                s.pendCoolBar = i + p2
        if s.phase == P_C and s.exc and not s.excTested and rk2:
            if s.outcome == ACC and low < s.excPrice and low >= rL2 - a * springMaxPenATR:
                s.excPrice, s.excBar, s.excTime = low, i, tm
                s.excEff, s.excSpr = max(nz(s.excEff), e_i), max(nz(s.excSpr), spr_i)
            elif s.outcome == DST and high > s.excPrice and high <= rH2 + a * springMaxPenATR:
                s.excPrice, s.excBar, s.excTime = high, i, tm
                s.excEff, s.excSpr = max(nz(s.excEff), e_i), max(nz(s.excSpr), spr_i)

        # SOS / SOW → Phase D
        if s.phase == P_C and rk2 and not s.pend:
            formal = not s.exc or s.excTested
            anchor = s.cReadyBar if not na(s.cReadyBar) else s.excBar
            ageReady = not na(anchor) and i - anchor >= phase_c_to_d_min_bars(p2)
            cVal = validation(p2, trendScore, a)
            cValD = cVal if formal else min(100, cVal + 13)
            cConf = conf()
            cConfD = cConf if formal else min(100, cConf + 30)
            cHt = max(rH2 - rL2, mintick)
            if ageReady and cValD >= phaseDValidationMin and cConfD >= minConfidencePhaseC:
                req = strengthMinScore if formal else max(2, strengthMinScore - 1)
                if s.outcome == ACC and ageOKBull:
                    dom = rL2 + cHt * phaseDDominanceFrac
                    brk = close > rH2 + a * breakATR
                    domOK = brk or (close >= dom and close1 >= dom and (formal or close2 >= dom))
                    if domOK and (sosScore >= req or mbSOS or (not formal and m["res3Up"][i] >= 1.0)):
                        promote_d(ACC, sosScore, high)
                        sig.add("STR")
                elif s.outcome == DST and ageOKBear:
                    dom = rH2 - cHt * phaseDDominanceFrac
                    brk = close < rL2 - a * breakATR
                    domOK = brk or (close <= dom and close1 <= dom and (formal or close2 <= dom))
                    if domOK and (sowScore >= req or mbSOW or (not formal and m["res3Dn"][i] >= 1.0)):
                        promote_d(DST, sowScore, low)
                        sig.add("STR")

        # Phase B departures (adopt a provisional Spring / UTAD, or abandon)
        resetLate = -1
        if s.phase == P_B and rk2 and not s.pend:
            dHt = max(rH2 - rL2, mintick)
            dBuf = max(dHt * phaseBDepartureTRFrac, a * phaseBDepartureATR)
            upAcc = close > rH2 + a * breakATR and close1 > rH2 + a * breakATR
            dnAcc = close < rL2 - a * breakATR and close1 < rL2 - a * breakATR
            upSus = m["loCDep"][i] > rH2 + dBuf
            dnSus = m["hiCDep"][i] < rL2 - dBuf
            upGo = (upAcc and (sosScore >= strengthMinScore or mbSOS)) or upSus
            dnGo = (dnAcc and (sowScore >= strengthMinScore or mbSOW)) or dnSus
            provLimit = phase_idle_limit(P_C, True, p2)
            if upGo and not dnGo:
                upProv = s.provEdge == ACC and not na(s.provBar) and i - s.provBar <= provLimit
                if ageOKBull and upProv and mature2:
                    adopt_prov()
                    promote_d(ACC, sosScore, high)
                    sig.add("STR")
                elif upSus and not upProv:
                    resetLate = 3
            elif dnGo and not upGo:
                dnProv = s.provEdge == DST and not na(s.provBar) and i - s.provBar <= provLimit
                if ageOKBear and dnProv and mature2:
                    adopt_prov()
                    promote_d(DST, sowScore, low)
                    sig.add("STR")
                elif dnSus and not dnProv:
                    resetLate = 3
        if resetLate >= 0:
            rs_counts[resetLate] += 1
            if s.cid >= 0:
                camps[s.cid]["why"] = RS[resetLate]
            s = WS()

        # renewed SOS / SOW after the LPS / LPSY
        if s.phase == P_D and s.outcome == ACC and s.ev == "LPS" and not na(s.rangeHigh):
            if close > s.rangeHigh + a * breakATR and relE >= strengthVolMult and cPos >= sosCloseMin:
                s.strBar, s.strTime, s.strPrice, s.strScore = i, tm, high, sosScore
                set_event("SOS")
                log("STR", f"SOS {sosScore}/4", i, high)
                sig.add("STR")
        if s.phase == P_D and s.outcome == DST and s.ev == "LPSY" and not na(s.rangeLow):
            if close < s.rangeLow - a * breakATR and relE >= strengthVolMult and cPos <= sowCloseMax:
                s.strBar, s.strTime, s.strPrice, s.strScore = i, tm, low, sowScore
                set_event("SOW")
                log("STR", f"SOW {sowScore}/4", i, low)
                sig.add("STR")

        # acceptance → Phase E
        if s.phase == P_D and not na(s.rangeHigh) and not na(s.rangeLow):
            eConf = conf()
            dAge = i - s.dStartBar if not na(s.dStartBar) else 0
            dMin = phase_d_min_bars(int(nz(s.pivLen, liveLen)))
            if s.outcome == ACC:
                s.outCount = s.outCount + 1 if close > s.rangeHigh else 0
                oppOK = regime != DST or s.cfLPS
                std = s.cfLPS and not na(s.lpsBar) and i - s.lpsBar >= confirmBars and dAge >= dMin and m["loCConf"][i] > s.rangeHigh and eConf >= minConfidencePhaseD
                drc = not s.cfLPS and dAge >= max(dMin, directAcceptanceBars) and m["loCDir"][i] > s.rangeHigh and eConf >= directAcceptanceConfidence
                if (std or drc) and oppOK:
                    s.cfAccept, s.phase, s.eStartBar, s.eStartTime = True, P_E, i, tm
                    regime, regimeBar = ACC, i
                    set_event("MARKUP")
                    log("E", "E markup", i, close)
            elif s.outcome == DST:
                s.outCount = s.outCount + 1 if close < s.rangeLow else 0
                oppOK = regime != ACC or s.cfLPS
                std = s.cfLPS and not na(s.lpsBar) and i - s.lpsBar >= confirmBars and dAge >= dMin and m["hiCConf"][i] < s.rangeLow and eConf >= minConfidencePhaseD
                drc = not s.cfLPS and dAge >= max(dMin, directAcceptanceBars) and m["hiCDir"][i] < s.rangeLow and eConf >= directAcceptanceConfidence
                if (std or drc) and oppOK:
                    s.cfAccept, s.phase, s.eStartBar, s.eStartTime = True, P_E, i, tm
                    regime, regimeBar = DST, i
                    set_event("MARKDOWN")
                    log("E", "E markdown", i, close)

        # entry qualification (Pine 1729–1747)
        if na(s.entryTime) and s.outcome != 0:
            eVal = validation(int(nz(s.pivLen, liveLen)), trendScore, a)
            quality = conf() >= eMinConf and eVal >= eValFloor and readiness(eMinConf, eValFloor, a) >= eMinTests
            testBit, strBit, lpsBit = "TEST" in sig, "STR" in sig, "LPS" in sig
            aggT = strictness == "Aggressive" and testBit and quality
            aggS = strictness == "Aggressive" and strBit and quality and na(s.testTime)
            stdT = strictness == "Standard" and testBit and quality and s.testScore >= testMinScore
            stdL = strictness == "Standard" and lpsBit and quality
            conL = strictness == "Conservative" and lpsBit and quality
            if aggT or aggS or stdT or stdL or conL:
                s.entryTime, s.entryPrice = tm, close
                s.entryKind = "lps" if (stdL or conL) else "strength" if aggS else "test"
                entries.append({"mode": "auto", "kind": s.entryKind, "dir": s.outcome, "bar": i, "known": i,
                                "price": close, "cid": s.cid})

        # event-trigger signals (Pine 2230–2234: stored once per event time)
        if s.outcome != 0:
            for mode, t_, p_ in (("test", s.testTime, s.testPrice), ("strength", s.strTime, s.strPrice),
                                 ("lps", s.lpsTime, s.lpsPrice), ("e", s.eStartTime, s.lastEventPrice)):
                if not na(t_) and (mode, t_, s.outcome) not in seen:
                    seen.add((mode, t_, s.outcome))
                    eb = bisect_left(T, t_)
                    entries.append({"mode": mode, "kind": mode, "dir": s.outcome, "bar": eb, "known": i, "price": float(p_),
                                    "cid": s.cid})

        # per-bar snapshot
        if s.cid >= 0:
            c = camps[s.cid]
            c["end"] = i
            if s.phase not in c["phase_bar"]:
                c["phase_bar"][s.phase] = i
            st_cid[i], st_phase[i] = s.cid, s.phase
            st_rh[i], st_rl[i] = s.rangeHigh, s.rangeLow
            wdir = s.outcome if s.outcome != 0 else bias_dir()
            st_col[i] = 0 if s.outcome == 0 else (1 if wdir == ACC else -1)

    return {"campaigns": camps, "entries": entries, "resets": dict(zip(RS, rs_counts)), "last": s,
            "type": type_name(s.stopSide, s.outcome if s.outcome != 0 else bias_dir(), s.ctxBull, s.ctxBear) if s.stopSide else "-",
            "atr": a_np, "state": {"cid": st_cid, "phase": st_phase, "rh": st_rh, "rl": st_rl, "col": st_col}, "tf_sec": tfs}


# ── per-chart tally ─────────────────────────────────────────────────────────────────────────────────────────────
def trade_entries(df: pd.DataFrame, sigs: list, m1: pd.DataFrame | None, tgt=30.0, sl=30.0, square_off=915,
                  cost=COST) -> list:
    """Trade each signal ({known: bar index, dir}) at the NEXT chart bar's open, same session day only, one trade at a
    time. Exits on 1-minute bars (m1; None → chart bars): stop first if both are hit in one bar, out at the first bar
    at / after `square_off` (its open). Pure."""
    if df.empty or not sigs:
        return []
    src = m1 if m1 is not None and len(m1) else df
    sT = src.index.values
    sO, sH, sL, sC = (src[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    sTod = (src.index.hour * 60 + src.index.minute).to_numpy()
    sDay = src.index.normalize().values
    idx = df.index
    trades, free_from = [], None
    for sg in sorted(sigs, key=lambda x: x["known"]):
        k = sg["known"]
        if k + 1 >= len(df):
            continue
        sig_t, entry_t = idx[k], idx[k + 1]
        if entry_t.normalize() != sig_t.normalize() or entry_t.hour * 60 + entry_t.minute >= square_off:
            continue
        if free_from is not None and entry_t < free_from:
            continue
        j0 = int(np.searchsorted(sT, np.datetime64(entry_t)))
        if j0 >= len(sT) or sDay[j0] != np.datetime64(entry_t.normalize()):
            continue
        j1 = int(np.searchsorted(sDay, sDay[j0], side="right"))
        side = sg["dir"]
        e = sO[j0]
        Tp, Sp = e + side * tgt, e - side * sl
        seg = slice(j0, j1)
        tod_s, H_s, L_s, O_s = sTod[seg], sH[seg], sL[seg], sO[seg]
        first = lambda mm: int(np.argmax(mm)) if mm.any() else len(mm)
        mx = first(tod_s >= square_off)
        ms = first(L_s <= Sp) if side > 0 else first(H_s >= Sp)
        mt = first(H_s >= Tp) if side > 0 else first(L_s <= Tp)
        tr = {**sg, "side": side, "sig_t": sig_t, "entry_t": entry_t, "entry": float(e), "target": Tp, "stop": Sp}
        mm = min(mx, ms, mt)
        if mm < len(tod_s):
            if mx == mm:
                tr.update(exit_t=pd.Timestamp(sT[j0 + mx]), exit=float(O_s[mx]), why=f"{square_off // 60}:{square_off % 60:02d}")
            elif ms <= mt:
                px = Sp if ms == 0 else (min(O_s[ms], Sp) if side > 0 else max(O_s[ms], Sp))
                tr.update(exit_t=pd.Timestamp(sT[j0 + ms]), exit=float(px), why="stop")
            else:
                tr.update(exit_t=pd.Timestamp(sT[j0 + mt]), exit=float(Tp), why="target")
        if "exit_t" in tr:
            tr["gross"] = side * (tr["exit"] - e)
            tr["net"] = tr["gross"] - cost * e
            free_from = tr["exit_t"] + pd.Timedelta(minutes=1)
        else:
            free_from = pd.Timestamp.max
        trades.append(tr)
    return trades


def tally(trades) -> str:
    done = [t for t in trades if "net" in t]
    if not done:
        return "no closed trades"
    net = np.array([t["net"] for t in done])
    tg = sum(t["why"] == "target" for t in done)
    st = sum(t["why"] == "stop" for t in done)
    return (f"{len(done)} trades · target {tg} / stop {st} / time {len(done) - tg - st} · win {round(100 * (net > 0).mean())}% · "
            f"avg {net.mean():+.1f} pts net · total {net.sum():+.0f} pts")


# ── indicator ───────────────────────────────────────────────────────────────────────────────────────────────────
COLS = {"AR": "#78909c", "ST": "#2962ff", "DEM": "#9e9e9e"}
BOX = {0: ("#2962ff", "#2962ff0d"), 1: ("#26a69a", "#26a69a0d"), -1: ("#ef5350", "#ef53500d")}


class Wyckoff(Indicator):
    id, name = "wyckoff", "Wyckoff (theUltimator5 port)"
    description = ("Port of the TradingView 'Wyckoff [theUltimator5]' engine on the chart timeframe: PS/SC/AR/ST, Phase B "
                   "tests, Spring/UTAD/Test, SOS/SOW, LPS/LPSY, Phase E, range box, BUY/SELL where the entry policy "
                   "qualifies. For STUDY — the entries are not a validated strategy; the tally (next-bar open, fixed "
                   "target/stop, 15:15 out, 0.02 % cost) only shows what they did on the loaded bars. HTF override skipped.")
    overlay = True
    warmup = 1500
    params = [Param("strictness", "choice", "Standard", "Entry strictness", options=["Conservative", "Standard", "Aggressive"]),
              Param("trigger", "choice", TRIGGERS[0], "Buy/Sell trigger", options=TRIGGERS),
              Param("min_phase", "choice", "B", "Minimum phase shown", options=list(PHASES)),
              Param("events", "bool", True, "Show event labels"),
              Param("history", "int", 0, "Draw last N campaigns (0 = all; trimming hides older ones in replay)", 0, 500),
              Param("at_event", "bool", False, "Draw BUY/SELL at the event bar (still revealed when qualified)"),
              Param("target", "float", 30.0, "Tally: target (points)", 1, 1000, 1),
              Param("stop", "float", 30.0, "Tally: stop (points)", 1, 1000, 1)]

    def compute(self, df, strictness, trigger, min_phase, events, history, at_event, target, stop):
        if df.empty or len(df) < 60:
            return Output(info={"note": "not enough bars"})
        sym = df.attrs.get("symbol")
        res = run_engine(df, strictness)
        t = times(df)
        n = len(df)
        mp = PHASES[min_phase]
        camps = res["campaigns"]
        shown = {c["cid"]: min(b for p, b in c["phase_bar"].items() if p >= mp) for c in camps
                 if any(p >= mp for p in c["phase_bar"])}
        if history > 0:
            keep = sorted(shown)[-history:]
            shown = {k: shown[k] for k in keep}
        shapes, markers = [], []
        # range box pieces
        stt = res["state"]
        prev, start, boxed = None, 0, set()
        for i in range(n + 1):
            key = None
            if i < n:
                cid = int(stt["cid"][i])
                if cid in shown and stt["phase"][i] >= mp and np.isfinite(stt["rh"][i]) and np.isfinite(stt["rl"][i]):
                    key = (cid, float(stt["rh"][i]), float(stt["rl"][i]), int(stt["col"][i]))
            if key != prev:
                if prev is not None:
                    cid, rh, rl, col = prev
                    first = cid not in boxed
                    boxed.add(cid)
                    left = t[camps[cid]["events"][0]["bar"]] if first and camps[cid]["events"] else t[start]
                    bc, bf = BOX[col]
                    shapes.append({"kind": "box", "points": [{"time": int(left), "price": rh}, {"time": int(t[i - 1]), "price": rl}],
                                   "color": bc, "fill": bf, "width": 1, "visible_from": int(t[start]), "clip": True})
                prev, start = key, i
        # event labels
        if events:
            for c in camps:
                if c["cid"] not in shown:
                    continue
                vis0 = shown[c["cid"]]
                acc = c["stop"] == ACC
                for ev in c["events"]:
                    k = ev["kind"]
                    if k in ("CL", "PS"):
                        col = "#00897b" if acc else "#ad1457"
                    elif k in COLS:
                        col = COLS[k]
                    else:
                        col = "#26a69a" if ev["dir"] == ACC else "#ef5350"
                    d = ev["dir"]
                    low = {"CL": acc, "PS": acc, "AR": not acc, "ST": acc, "EXC": d == ACC, "TEST": d == ACC,
                           "LPS": d == ACC, "STR": d != ACC, "E": d != ACC}.get(k, False)
                    y = ev["price"] + (-0.6 if low else 0.6) * float(res["atr"][ev["bar"]])
                    shapes.append({"kind": "label", "points": [{"time": int(t[ev["bar"]]), "price": y}],
                                   "color": col, "text": ev["text"],
                                   "visible_from": int(t[max(ev["known"], vis0)])})
        # entries
        mode = {"Auto (Entry Strictness)": "auto", "Phase C Test": "test", "SOS / SOW": "strength",
                "LPS / LPSY": "lps", "Phase E": "e"}[trigger]
        sigs = [e for e in res["entries"] if e["mode"] == mode]
        for e in sigs:
            up = e["dir"] == ACC
            mt = int(t[e["bar"] if at_event else e["known"]])
            name = ENTRY_NAME.get(e["kind"], "") if mode == "auto" else \
                {"test": "Phase C Test", "strength": "SOS" if up else "SOW", "lps": "LPS" if up else "LPSY", "e": "Phase E"}[mode]
            markers.append({"time": mt, "position": "belowBar" if up else "aboveBar",
                            "shape": "arrowUp" if up else "arrowDown", "color": "#26a69a" if up else "#ef5350",
                            "text": f"{'BUY' if up else 'SELL'} · {name}", "visible_from": int(t[e["known"]])})
        # tally
        info = {"about": "Port of Wyckoff [theUltimator5] for study — entries are NOT a validated strategy.",
                "rules": f"{strictness} · trigger {trigger} · tally: next-bar open, target {target:g} / stop {stop:g} pts, "
                         "stop first on a tie, same-day square-off, cost 0.02 %"}
        s = symbols.all_symbols().get(sym) if sym else None
        se = store.sess(sym) if sym else (555, 930)
        sq = 915 if (s is None or s.nse_hours) else (s.square_off or se[1] - 15)
        if se[1] <= 1440 and se[0] < se[1]:
            step = np.diff(df.index.values).astype("timedelta64[m]").astype(int)
            tfm = int(np.median(step[step > 0])) if (step > 0).any() else 1
            m1 = None
            if sym and tfm > 1:
                try:
                    m1 = store.bars(sym, "1")
                    m1 = m1[(m1.index >= df.index[0]) & (m1.index < df.index[-1] + pd.Timedelta(minutes=tfm))]
                except Exception:
                    m1 = None
            trades = trade_entries(df, sigs, m1, float(target), float(stop), sq)
            tpos = lambda ts: int(t[max(0, int(df.index.searchsorted(pd.Timestamp(ts), side="right")) - 1)])
            for tr in trades:
                if "net" in tr:
                    end = tpos(tr["exit_t"])
                    ok = tr["net"] > 0
                    markers.append({"time": end, "position": "aboveBar" if tr["side"] > 0 else "belowBar", "shape": "square",
                                    "color": "#26a69a" if ok else "#ef5350", "visible_from": end,
                                    "text": f"{'✓' if ok else '✗'} {tr['why']} {tr['gross']:+.1f}"})
            info["on this chart"] = tally(trades)
        else:
            info["on this chart"] = "tally only for sessions inside one calendar day (NSE / MCX)"
        st = res["last"]
        if st.stopSide:
            info["structure"] = (f"{res['type']} · phase {LETTER[st.phase]} · range "
                                 f"{st.rangeHigh:,.2f} / {st.rangeLow:,.2f}" if np.isfinite(st.rangeHigh) and np.isfinite(st.rangeLow)
                                 else f"{res['type']} · phase {LETTER[st.phase]} · range forming")
            info["last event"] = st.ev
        else:
            info["structure"] = "searching for SC / BC"
        info["campaigns"] = (f"{len(camps)} seeded · {sum(1 for c in camps if max(c['phase_bar']) >= P_B)} reached B · "
                             f"{sum(1 for c in camps if max(c['phase_bar']) >= P_D)} reached D · "
                             f"{sum(1 for c in camps if max(c['phase_bar']) >= P_E)} reached E")
        info["signals"] = " · ".join(f"{k} {sum(1 for e in res['entries'] if e['mode'] == k)}" for k in ("auto", "test", "strength", "lps", "e"))
        info["resets"] = " · ".join(f"{k} {v}" for k, v in res["resets"].items() if v)
        return Output(shapes=shapes, markers=markers, info=info)


INDICATORS = [Wyckoff()]
