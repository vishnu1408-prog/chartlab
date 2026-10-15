"""Harmonic pattern scanner for the Pattern Coach (auto-mark + challenge).

Uses the same ratio specs as the harmonic indicator (lib/harmonic_ref.py). Only CONFIRMED swing points are used: a
pivot of length L is known L bars after it happened, so a scan that ends at time `end` never sees the future.

scan()        completed patterns (X-A-B-C-D all confirmed) and forming ones (X-A-B-C confirmed, D zone projected),
              each with a plain-language ratio checklist and, when the data allows, what happened after D.
next_setup()  the next moment after `after` where X-A-B-C of a valid pattern is confirmed — the challenge round —
              plus how it resolved (the actual D), returned separately so the UI can hide it until the reveal.
"""
import numpy as np
import pandas as pd

from . import store
from .indicators.lib import harmonic_ref as hr

NAMES = hr.NAMES
KEYS = ["gartley", "bat", "altbat", "butterfly", "crab", "deepcrab", "shark", "cypher", "abcd"]
ABCD, CYPHER = hr.ABCD, hr.CYPHER


def zigzag(df: pd.DataFrame, L: int):
    """Alternating swing points [(price, bar, dir, confirmed_bar)] using only data in df (dir 1 = high, -1 = low)."""
    h, l = df.high.tolist(), df.low.tolist()
    ph, pl = hr.pivots(h, L, True), hr.pivots(l, L, False)
    P = []
    for i in range(len(h)):
        first_low = bool(P) and P[-1][2] == 1
        for val, d in ([(pl[i], -1), (ph[i], 1)] if first_low else [(ph[i], 1), (pl[i], -1)]):
            if val is None:
                continue
            if P and P[-1][2] == d:
                if (d == 1 and val > P[-1][0]) or (d == -1 and val < P[-1][0]):
                    P[-1] = (val, i - L, d, i)
            else:
                P.append((val, i - L, d, i))
    return P


def _rng(lo, hi):
    return f"{lo:.3f}" if abs(lo - hi) < 1e-9 else f"{lo:.3f}–{hi:.3f}"


def checks(pid, x, a, b, c, d=None, tol=0.08):
    """Plain-language ratio checklist. Returns (list, all_ok)."""
    s = hr.SPEC[pid]
    xa, ab, bc = abs(a - x), abs(b - a), abs(c - b)
    ok_in = lambda r, lo, hi: lo * (1 - tol) <= r <= hi * (1 + tol)
    out = []
    if pid != ABCD:
        r = ab / xa if xa else 0
        out.append({"leg": "B", "text": f"B retraced {r * 100:.1f}% of XA", "value": round(r, 3), "need": _rng(s[0], s[1]), "ok": ok_in(r, s[0], s[1])})
    if pid == CYPHER:
        r = abs(c - x) / xa if xa else 0
        out.append({"leg": "C", "text": f"C extended to {r * 100:.1f}% of XA (beyond A)", "value": round(r, 3), "need": _rng(s[2], s[3]), "ok": ok_in(r, s[2], s[3])})
    else:
        r = bc / ab if ab else 0
        out.append({"leg": "C", "text": f"C retraced {r * 100:.1f}% of AB", "value": round(r, 3), "need": _rng(s[2], s[3]), "ok": ok_in(r, s[2], s[3])})
    if d is not None:
        if pid == CYPHER:
            r = abs(d - c) / abs(c - x) if c != x else 0
            txt = f"D retraced {r * 100:.1f}% of XC"
        elif pid == ABCD:
            r = abs(d - c) / ab if ab else 0
            txt = f"CD is {r * 100:.1f}% of AB"
        else:
            r = abs(d - a) / xa if xa else 0
            txt = f"D is at {r * 100:.1f}% of XA"
        out.append({"leg": "D", "text": txt, "value": round(r, 3), "need": _rng(s[6], s[7]), "ok": ok_in(r, s[6], s[7])})
        if pid not in (CYPHER, ABCD):
            r = abs(d - c) / bc if bc else 0
            out.append({"leg": "BC", "text": f"CD is {r:.2f}× BC (projection, info)", "value": round(r, 3), "need": _rng(s[4], s[5]),
                        "ok": ok_in(r, s[4], s[5]), "info": True})
    return out, all(x["ok"] for x in out if not x.get("info"))


def _pt(t, i, price, label):
    return {"time": int(t[i]), "price": round(float(price), 2), "label": label}


def _reaction(df, d_bar, a, d, dirn, bars=30):
    """How far price came back toward A after D (fraction of AD) within `bars` bars, and the worst move against."""
    seg = df.iloc[d_bar + 1: d_bar + 1 + bars]
    if seg.empty:
        return None
    ad = abs(a - d) or 1e-9
    fav = (seg.high.max() - d) if dirn == 1 else (d - seg.low.min())
    adv = (d - seg.low.min()) if dirn == 1 else (seg.high.max() - d)
    return {"retrace": round(float(fav / ad), 3), "against": round(float(adv / ad), 3), "bars": int(len(seg)),
            "t1_hit": bool(fav / ad >= 0.382), "complete": len(seg) >= bars}


def scan(df: pd.DataFrame, piv_len=3, tol=0.08, enabled=None, max_items=40) -> dict:
    enabled = set(enabled or KEYS)
    if len(df) < 30:
        return {"completed": [], "forming": []}
    t = store.to_epoch(df.index)
    P = zigzag(df, piv_len)
    completed, forming = [], []
    for k in range(3, len(P)):
        for pid, key in enumerate(KEYS):
            if key not in enabled:
                continue
            if pid == ABCD:
                pts = P[k - 3: k + 1]
                x = None
                a, b, c, d = (p[0] for p in pts)
            else:
                if k < 4:
                    continue
                pts = P[k - 4: k + 1]
                x, a, b, c, d = (p[0] for p in pts)
            # bullish = D is a low
            dirn = 1 if pts[-1][2] == -1 else -1
            cl, ok = checks(pid, x if x is not None else a, a, b, c, d, tol) if pid != ABCD else checks(pid, a, a, b, c, d, tol)
            if pid == ABCD:
                cl = [y for y in cl if y["leg"] != "B"]
                ok = all(y["ok"] for y in cl if not y.get("info"))
            if not ok:
                continue
            labels = ["A", "B", "C", "D"] if pid == ABCD else ["X", "A", "B", "C", "D"]
            d_bar = pts[-1][1]
            item = {"pattern": NAMES[pid], "key": key, "dir": "bullish" if dirn == 1 else "bearish",
                    "points": [_pt(t, p[1], p[0], lb) for p, lb in zip(pts, labels)], "checks": cl,
                    "known_at": int(t[pts[-1][3]]), "score": hr.score(pid, x if x is not None else a, a, b, c, d, tol) if pid != ABCD else 100,
                    "reaction": _reaction(df, d_bar, a, d, dirn)}
            completed.append(item)
    # forming: the last confirmed pivot is C
    if len(P) >= 3:
        last_close = float(df.close.iloc[-1])
        for pid, key in enumerate(KEYS):
            if key not in enabled:
                continue
            need = 3 if pid == ABCD else 4
            if len(P) < need:
                continue
            pts = P[-need:]
            if pid == ABCD:
                a, b, c = (p[0] for p in pts); x = a
            else:
                x, a, b, c = (p[0] for p in pts)
            ok, top, bot = hr.prz(pid, x, a, b, c, tol, requireBC=False)
            if not ok:
                continue
            dirn = 1 if pts[-1][2] == 1 else -1            # C is a high → D will be a low → bullish
            after = df.iloc[pts[-1][1] + 1:]
            broken = (after.high.max() > c) if dirn == 1 else (after.low.min() < c) if len(after) else False
            far = bot if dirn == 1 else top
            overshoot = (after.low.min() < far - 0.25 * abs(top - bot) - 1e-9) if dirn == 1 else (after.high.max() > far + 0.25 * abs(top - bot) + 1e-9) if len(after) else False
            if broken or overshoot:
                continue
            cl, _ = checks(pid, x, a, b, c, None, tol)
            if pid == ABCD:
                cl = [y for y in cl if y["leg"] != "B"]
            labels = ["A", "B", "C"] if pid == ABCD else ["X", "A", "B", "C"]
            inside = bot <= last_close <= top
            forming.append({"pattern": NAMES[pid], "key": key, "dir": "bullish" if dirn == 1 else "bearish",
                            "points": [_pt(t, p[1], p[0], lb) for p, lb in zip(pts, labels)], "checks": cl,
                            "prz": [round(float(bot), 2), round(float(top), 2)], "known_at": int(t[pts[-1][3]]),
                            "status": "price is IN the zone — watch for a reversal candle" if inside else
                                      f"waiting: {abs(last_close - (top if dirn == 1 else bot)):.1f} pts to the zone"})
    completed.sort(key=lambda x: x["known_at"])
    return {"completed": completed[-max_items:], "forming": forming}


def next_setup(df: pd.DataFrame, after: int, piv_len=3, tol=0.08, enabled=None, max_bars=1500) -> dict | None:
    """First time after `after` when X-A-B-C (C confirmed) fits a pattern, and how D actually turned out."""
    enabled = set(enabled or KEYS) - {"abcd"}
    t = store.to_epoch(df.index)
    P = zigzag(df, piv_len)
    for k in range(3, len(P) - 1):
        conf = P[k][3]
        if t[conf] <= after or conf >= len(df) - 2:
            continue
        if t[conf] > after + max_bars * 60 * 15:
            break
        x, a, b, c = (p[0] for p in P[k - 3: k + 1])
        cands = []
        for pid, key in enumerate(KEYS):
            if key not in enabled:
                continue
            ok, top, bot = hr.prz(pid, x, a, b, c, tol, requireBC=False)
            if ok:
                cands.append((pid, key, float(bot), float(top)))
        if not cands:
            continue
        dirn = 1 if P[k][2] == 1 else -1                   # C high → bullish (D low)
        # the actual D: the furthest point reached after C, confirmed once price bounces back 50% of the C→D leg
        # (or turns back through C); max 150 bars
        c_bar = P[k][1]
        seg = df.iloc[c_bar + 1: c_bar + 151]
        hi_, lo_ = seg.high.to_numpy(), seg.low.to_numpy()
        ext, ext_i, end_i, broke = None, None, len(seg) - 1, False
        for j in range(len(seg)):
            if j > 0 and ext_i is not None:
                leg = abs(ext - c)
                back = (hi_[j] - ext) if dirn == 1 else (ext - lo_[j])
                if ((dirn == -1 and lo_[j] < c) or (dirn == 1 and hi_[j] > c)) or (leg > 0 and back >= 0.5 * leg):
                    end_i, broke = j, True
                    break
            v = lo_[j] if dirn == 1 else hi_[j]
            if ext is None or (v < ext if dirn == 1 else v > ext):
                ext, ext_i = v, j
        if ext is None or c_bar + 1 + end_i <= conf:
            continue
        best = min(cands, key=lambda z: abs(c - (z[2] + z[3]) / 2))   # the zone price reaches first
        d_bar = c_bar + 1 + ext_i
        res_bar = c_bar + 1 + end_i
        rx = _reaction(df, d_bar, a, ext, dirn)
        labels = ["X", "A", "B", "C"]
        lo, hi = best[2], best[3]
        return {
            "start_time": int(t[conf]),                     # replay shows bars up to and including this one
            "dir": "bullish" if dirn == 1 else "bearish",
            "points": [_pt(t, p[1], p[0], lb) for p, lb in zip(P[k - 3: k + 1], labels)],
            "app": {"pattern": NAMES[best[0]], "prz": [round(lo, 2), round(hi, 2)], "guess": round((lo + hi) / 2, 2),
                    "candidates": [{"pattern": NAMES[p], "prz": [round(a_, 2), round(b_, 2)]} for p, _, a_, b_ in cands]},
            "actual": {"time": int(t[d_bar]), "price": round(float(ext), 2), "resolved_at": int(t[res_bar]) + 1,
                       "turned_back": broke, "reaction": rx,
                       "in_zone": bool(lo - (hi - lo) * 0.25 <= ext <= hi + (hi - lo) * 0.25)},
        }
    return None
