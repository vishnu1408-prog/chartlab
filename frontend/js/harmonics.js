// Harmonic maths for the practice tools — a JS mirror of backend/app/indicators/lib/harmonic_ref.py and
// harmonic_scan.checks(), so the browser can grade drawings, preview D zones and score trade plans without a round trip.
// Pure functions only (unit-tested in tests/core.test.mjs).
import { minuteOfDay, dayOf, SQUARE_OFF_MIN } from "./core.js";

export const H_NAMES = ["Gartley", "Bat", "Alt Bat", "Butterfly", "Crab", "Deep Crab", "Shark", "Cypher", "AB=CD"];
export const CYPHER = 7, ABCD = 8;
// per pattern: xabLo, xabHi, r2Lo, r2Hi, bcdLo, bcdHi, dLo, dHi
//   r2 = BC/AB (Cypher: XC/XA);  d = AD/XA (Cypher: CD/XC, AB=CD: CD/AB, X ignored)
export const SPEC = [
  [0.618, 0.618, 0.382, 0.886, 1.272, 1.618, 0.786, 0.786],
  [0.382, 0.500, 0.382, 0.886, 1.618, 2.618, 0.886, 0.886],
  [0.236, 0.382, 0.382, 0.886, 2.000, 3.618, 1.130, 1.130],
  [0.786, 0.786, 0.382, 0.886, 1.618, 2.618, 1.272, 1.414],
  [0.382, 0.618, 0.382, 0.886, 2.240, 3.618, 1.618, 1.618],
  [0.886, 0.886, 0.382, 0.886, 2.000, 3.618, 1.618, 1.618],
  [0.382, 0.886, 1.130, 1.618, 1.618, 2.240, 0.886, 1.130],
  [0.382, 0.618, 1.272, 1.414, 0.000, 100.0, 0.786, 0.786],
  [0.000, 100.0, 0.382, 0.886, 1.130, 2.618, 1.000, 1.272],
];
export const TOL = 0.08;
export const PATTERN_COLORS = ["#2962ff", "#26a69a", "#00bcd4", "#ab47bc", "#ef5350", "#ff7043", "#ffca28", "#8d6e63", "#78909c"];

const inRange = (r, lo, hi, tol) => lo * (1 - tol) <= r && r <= hi * (1 + tol);
const need = (lo, hi) => (Math.abs(lo - hi) < 1e-9 ? lo.toFixed(3) : hi >= 100 ? `≥ ${lo.toFixed(3)}` : `${lo.toFixed(3)}–${hi.toFixed(3)}`);

export function dLeg(pid, x, a, b, c) {
  if (pid === CYPHER) return [c, c - x];
  if (pid === ABCD) return [c, a - b];
  return [a, a - x];
}

/** D zone (Potential Reversal Zone) from X-A-B-C, primary D ratio only (like the coach's forming patterns).
 *  {ok: B and C ratios fit, lo, hi}. AB=CD uses A-B-C (x is ignored) and the overlap with the BC projection. */
export function prz(pid, x, a, b, c, tol = TOL) {
  const s = SPEC[pid];
  const xa = Math.abs(a - x), ab = Math.abs(b - a), bc = Math.abs(c - b);
  let ok = (pid === ABCD || xa > 0) && ab > 0 && bc > 0;
  const xab = xa ? ab / xa : 0;
  const r2 = pid === CYPHER ? (xa ? Math.abs(c - x) / xa : 0) : ab ? bc / ab : 0;
  ok = ok && (pid === ABCD || inRange(xab, s[0], s[1], tol)) && inRange(r2, s[2], s[3], tol);
  const [base, leg] = dLeg(pid, x, a, b, c);
  const d1 = base - s[6] * (1 - tol) * leg, d2 = base - s[7] * (1 + tol) * leg;
  let lo = Math.min(d1, d2), hi = Math.max(d1, d2);
  if (pid === ABCD) {
    const e1 = c - s[4] * (1 - tol) * (c - b), e2 = c - s[5] * (1 + tol) * (c - b);
    lo = Math.max(lo, Math.min(e1, e2)); hi = Math.min(hi, Math.max(e1, e2));
  }
  return { ok: ok && lo <= hi, lo, hi };
}

/** Plain-language checklist for one pattern (same wording and rules as harmonic_scan.checks). d may be null. */
export function checks(pid, x, a, b, c, d = null, tol = TOL) {
  const s = SPEC[pid];
  const xa = Math.abs(a - x), ab = Math.abs(b - a), bc = Math.abs(c - b);
  const out = [];
  const add = (leg, text, r, lo, hi, info = false) => out.push({ leg, text, value: Math.round(r * 1000) / 1000, need: need(lo, hi), ok: inRange(r, lo, hi, tol), info });
  if (pid !== ABCD) { const r = xa ? ab / xa : 0; add("B", `B retraced ${(r * 100).toFixed(1)}% of XA`, r, s[0], s[1]); }
  if (pid === CYPHER) { const r = xa ? Math.abs(c - x) / xa : 0; add("C", `C extended to ${(r * 100).toFixed(1)}% of XA (beyond A)`, r, s[2], s[3]); }
  else { const r = ab ? bc / ab : 0; add("C", `C retraced ${(r * 100).toFixed(1)}% of AB`, r, s[2], s[3]); }
  if (d != null) {
    let r, txt;
    if (pid === CYPHER) { r = c !== x ? Math.abs(d - c) / Math.abs(c - x) : 0; txt = `D retraced ${(r * 100).toFixed(1)}% of XC`; }
    else if (pid === ABCD) { r = ab ? Math.abs(d - c) / ab : 0; txt = `CD is ${(r * 100).toFixed(1)}% of AB`; }
    else { r = xa ? Math.abs(d - a) / xa : 0; txt = `D is at ${(r * 100).toFixed(1)}% of XA`; }
    add("D", txt, r, s[6], s[7]);
    if (pid !== CYPHER && pid !== ABCD) { const r2 = bc ? Math.abs(d - c) / bc : 0; add("BC", `CD is ${r2.toFixed(2)}× BC (projection, info)`, r2, s[4], s[5], true); }
  }
  return { list: out, ok: out.filter((y) => !y.info).every((y) => y.ok) };
}

/** Which patterns does a drawing fit? prices = [X, A, B, C, (D)] or, for AB=CD, [A, B, C, (D)].
 *  Returns one row per pattern: {pid, name, ok, checks}. Shape check: the legs must alternate (zig-zag). */
export function classify(prices, { abcd = false, tol = TOL } = {}) {
  const P = prices.filter((v) => v != null && isFinite(v));
  const rows = [];
  if (abcd) {
    if (P.length < 3) return rows;
    const [a, b, c, d] = P;
    const zig = (b - a) * (c - b) < 0 && (d == null || (d - c) * (c - b) < 0);
    const ch = checks(ABCD, a, a, b, c, d ?? null, tol);
    rows.push({ pid: ABCD, name: H_NAMES[ABCD], ok: zig && ch.ok, checks: ch.list.filter((y) => y.leg !== "B"), zig });
    return rows;
  }
  if (P.length < 4) return rows;
  const [x, a, b, c, d] = P;
  const zig = (a - x) * (b - a) < 0 && (c - b) * (b - a) < 0 && (d == null || (d - c) * (c - b) < 0);
  for (let pid = 0; pid < 8; pid++) {
    const ch = checks(pid, x, a, b, c, d ?? null, tol);
    rows.push({ pid, name: H_NAMES[pid], ok: zig && ch.ok, checks: ch.list, zig });
  }
  return rows;
}

/** D zones to preview while drawing: every pattern whose B and C fit. prices = [X, A, B, C] (AB=CD: [A, B, C]). */
export function dZones(prices, { abcd = false, only = null, tol = TOL } = {}) {
  if (abcd) {
    const [a, b, c] = prices;
    const z = prz(ABCD, a, a, b, c, tol);
    return z.ok ? [{ pid: ABCD, name: H_NAMES[ABCD], lo: z.lo, hi: z.hi, color: PATTERN_COLORS[ABCD] }] : [];
  }
  const [x, a, b, c] = prices;
  if ((a - x) * (b - a) >= 0 || (c - b) * (b - a) >= 0) return [];
  const out = [];
  for (let pid = 0; pid < 8; pid++) {
    if (only != null && !only.includes(pid)) continue;
    const z = prz(pid, x, a, b, c, tol);
    if (z.ok) out.push({ pid, name: H_NAMES[pid], lo: z.lo, hi: z.hi, color: PATTERN_COLORS[pid] });
  }
  return out;
}

// ───────────────────────────── swing points ─────────────────────────────
/** Pine-like pivots + alternating zigzag (port of harmonic_scan.zigzag): [{i, time, price, dir: 1 high | -1 low, conf}] */
export function zigzag(bars, L = 3) {
  const n = bars.length, P = [];
  const piv = (i, high) => {
    const v = high ? bars[i].high : bars[i].low;
    for (let k = i - L; k <= i + L; k++) {
      if (k === i) continue;
      const o = high ? bars[k].high : bars[k].low;
      if (high ? !(v > o) : !(v < o)) return null;
    }
    return v;
  };
  for (let j = 2 * L; j < n; j++) {                      // a pivot at i = j - L is known at bar j
    const i = j - L;
    const ph = piv(i, true), pl = piv(i, false);
    const firstLow = P.length && P[P.length - 1].dir === 1;
    for (const [val, d] of firstLow ? [[pl, -1], [ph, 1]] : [[ph, 1], [pl, -1]]) {
      if (val == null) continue;
      const last = P[P.length - 1];
      if (last && last.dir === d) {
        if ((d === 1 && val > last.price) || (d === -1 && val < last.price)) P[P.length - 1] = { i, time: bars[i].time, price: val, dir: d, conf: j };
      } else P.push({ i, time: bars[i].time, price: val, dir: d, conf: j });
    }
  }
  return P;
}

const nearestIndex = (times, t) => {
  let lo = 0, hi = times.length - 1;
  if (hi < 0) return -1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (times[m] < t) lo = m + 1; else hi = m; }
  if (lo > 0 && Math.abs(times[lo - 1] - t) <= Math.abs(times[lo] - t)) lo--;
  return lo;
};

/** "Check my drawing": match each clicked point to the real swing high/low the user meant.
 *  points = [{time, price}], kinds = [1 high | -1 low] per point. A point matches the nearest swing of its kind within
 *  `maxBars` bars. Returns per point {real: {time, price} | null, barsOff (+ = you were late), ptsOff, exact}. */
export function snapToSwings(points, kinds, bars, { L = 3, maxBars = 8 } = {}) {
  const times = bars.map((b) => b.time);
  const Z = zigzag(bars, L);
  // also accept smaller swings (L = 2): a person often marks a 2-bar swing that a 3-bar zigzag merges away
  const Z2 = zigzag(bars, 2);
  return points.map((p, k) => {
    const bi = nearestIndex(times, p.time);
    if (bi < 0) return { real: null, barsOff: null, ptsOff: null, exact: false };
    let best = null;
    for (const [list, pen] of [[Z, 0], [Z2, 0.5]]) {
      for (const s of list) {
        if (s.dir !== kinds[k]) continue;
        const db = Math.abs(s.i - bi);
        if (db > maxBars) continue;
        const score = db + pen + Math.abs(s.price - p.price) / Math.max(1e-9, Math.abs(bars[bi].high - bars[bi].low) * 4 + 1);
        if (!best || score < best.score) best = { s, score };
      }
    }
    if (!best) return { real: null, barsOff: null, ptsOff: null, exact: false };
    const s = best.s;
    const barsOff = bi - s.i, ptsOff = p.price - s.price;
    return { real: { time: s.time, price: s.price }, barsOff, ptsOff, exact: barsOff === 0 && Math.abs(ptsOff) <= Math.max(0.5, s.price * 0.00002) };
  });
}

/** Compare a drawing with known target points (daily-drill "mark" task). */
export function gradeAgainst(points, target, bars) {
  const times = bars.map((b) => b.time);
  return points.map((p, k) => {
    const t = target[k];
    if (!t) return { real: null, barsOff: null, ptsOff: null, exact: false };
    const barsOff = nearestIndex(times, p.time) - nearestIndex(times, t.time);
    const ptsOff = p.price - t.price;
    return { real: { time: t.time, price: t.price }, barsOff, ptsOff, exact: barsOff === 0 && Math.abs(ptsOff) <= Math.max(0.5, t.price * 0.00002) };
  });
}

/** high (1) or low (-1) for each point of a zig-zag: the first point is the opposite of the second. */
export function kindsOf(prices) {
  return prices.map((v, i) => {
    const nb = i === 0 ? prices[1] : prices[i - 1];
    return v >= nb ? 1 : -1;
  });
}

// ───────────────────────────── trade-plan drill ─────────────────────────────
/** Score a trade plan on the bars after `from` (only bars already revealed are passed in during replay).
 *  plan = {side: 1 long | -1 short, entry, stop, target, from, cancel (price beyond C that kills the setup), maxBars}
 *  Rules (intraday, like the practice engine): the entry is a limit order at `entry`; it may fill on any later
 *  session bar before 15:15 (a gap fills at the open); an open position is closed at 15:15; stop beats target inside
 *  one bar; on the fill bar only the stop is checked (we cannot know the order of moves inside a bar).
 *  The order is cancelled if price trades through `cancel` before filling, or after `maxBars` bars without a fill.
 *  Returns {status: "waiting" | "open" | "done" | "cancelled" | "expired", ...}. R = result / (entry-stop distance). */
export function simulatePlan(bars, plan) {
  const { side, entry, stop, target, from, cancel = null, maxBars = 150 } = plan;
  const risk = Math.abs(entry - stop);
  let fill = null, n = 0;
  for (const b of bars) {
    if (b.time <= from) continue;
    const m = minuteOfDay(b.time);
    if (!fill) {
      n++;
      if (n > maxBars) return { status: "expired", reason: `no fill within ${maxBars} bars` };
      if (cancel != null && (side > 0 ? b.high >= cancel : b.low <= cancel)) return { status: "cancelled", reason: "price broke beyond C before reaching your entry — setup dead", time: b.time };
      if (m >= SQUARE_OFF_MIN) continue;                                     // no new entries from 15:15
      const touch = side > 0 ? b.low <= entry : b.high >= entry;
      if (!touch) continue;
      const px = side > 0 ? Math.min(b.open, entry) : Math.max(b.open, entry);
      fill = { time: b.time, price: px, day: dayOf(b.time) };
      if (side > 0 ? b.low <= stop : b.high >= stop) return done(fill, stop, b.time, "stop (same bar)");
      continue;
    }
    if (dayOf(b.time) !== fill.day || m >= SQUARE_OFF_MIN) return done(fill, b.open, b.time, "square-off 15:15");
    const gapS = side > 0 ? b.open <= stop : b.open >= stop, hitS = side > 0 ? b.low <= stop : b.high >= stop;
    if (gapS || hitS) return done(fill, gapS ? b.open : stop, b.time, "stop");
    const gapT = side > 0 ? b.open >= target : b.open <= target, hitT = side > 0 ? b.high >= target : b.low <= target;
    if (gapT || hitT) return done(fill, gapT ? b.open : target, b.time, "target");
  }
  return fill ? { status: "open", fill } : { status: "waiting" };

  function done(f, px, time, reason) {
    const pts = (px - f.price) * side;
    return { status: "done", fill: f, exit: { time, price: px }, reason, pts, R: risk ? pts / risk : 0 };
  }
}

/** Is a plan's geometry sensible? (long: stop < entry < target) — returns an error message or null. */
export function planError(side, entry, stop, target) {
  if ([entry, stop, target].some((v) => v == null || !isFinite(v))) return "set entry, stop and target";
  if (side > 0 && !(stop < entry && entry < target)) return "for a long: stop below entry, target above entry";
  if (side < 0 && !(stop > entry && entry > target)) return "for a short: stop above entry, target below entry";
  return null;
}

// ───────────────────────────── practice statistics ─────────────────────────────
/** Summarise practice-log entries: per mode, per pattern, mistakes per point, confusions, plan R, days. */
export function practiceStats(log) {
  const S = { total: log.length, modes: {}, patterns: {}, points: {}, confusion: {}, plan: { n: 0, wins: 0, R: 0, filled: 0 }, days: {} };
  for (const e of log) {
    const m = (S.modes[e.mode] ||= { n: 0, ok: 0 });
    m.n++; if (e.ok) m.ok++;
    const day = (e.at || "").slice(0, 10);
    if (day) { const d = (S.days[day] ||= { n: 0, ok: 0 }); d.n++; if (e.ok) d.ok++; }
    if (e.pattern) { const p = (S.patterns[e.pattern] ||= { n: 0, ok: 0 }); p.n++; if (e.ok) p.ok++; }
    if (e.answer && e.pattern && e.answer !== e.pattern && !e.ok) { const k = `${e.pattern} → you said ${e.answer}`; S.confusion[k] = (S.confusion[k] || 0) + 1; }
    for (const pt of e.points || []) {
      if (pt.barsOff == null) continue;
      const q = (S.points[pt.label] ||= { n: 0, exact: 0, early: 0, late: 0, absBars: 0, absPts: 0, missed: 0 });
      q.n++; if (pt.exact) q.exact++; if (pt.barsOff < 0) q.early++; if (pt.barsOff > 0) q.late++;
      q.absBars += Math.abs(pt.barsOff); q.absPts += Math.abs(pt.ptsOff || 0);
    }
    for (const pt of e.points || []) if (pt.barsOff == null && pt.label) { const q = (S.points[pt.label] ||= { n: 0, exact: 0, early: 0, late: 0, absBars: 0, absPts: 0, missed: 0 }); q.missed++; }
    if (e.mode === "plan") {
      S.plan.n++;
      if (e.status === "done") { S.plan.filled++; S.plan.R += e.R || 0; if ((e.R || 0) > 0) S.plan.wins++; }
    }
  }
  return S;
}

/** Plain-language list of the biggest habits in the log (for the stats card). */
export function mistakeHints(S) {
  const out = [];
  for (const [lb, q] of Object.entries(S.points)) {
    const tot = q.n + q.missed;
    if (tot < 3) continue;
    if (q.missed / tot >= 0.25) out.push(`You often put ${lb} where there is no real swing (${Math.round((100 * q.missed) / tot)}% of the time).`);
    if (q.n && q.late / q.n >= 0.4) out.push(`You mark ${lb} too late ${Math.round((100 * q.late) / q.n)}% of the time (avg ${(q.absBars / q.n).toFixed(1)} bars off).`);
    if (q.n && q.early / q.n >= 0.4) out.push(`You mark ${lb} too early ${Math.round((100 * q.early) / q.n)}% of the time (avg ${(q.absBars / q.n).toFixed(1)} bars off).`);
  }
  const conf = Object.entries(S.confusion).sort((a, b) => b[1] - a[1]).slice(0, 3);
  for (const [k, n] of conf) if (n >= 2) out.push(`Confusion: ${k} (${n}×).`);
  if (S.plan.filled >= 5 && S.plan.R / S.plan.filled < 0) out.push(`Your trade plans average ${(S.plan.R / S.plan.filled).toFixed(2)}R — check stop placement (too tight?) and targets (too far?).`);
  return out;
}

/** consecutive days (ending today or yesterday) with a completed daily drill */
export function drillStreak(dates, today) {
  const set = new Set(dates);
  const d = new Date(today + "T00:00:00Z");
  if (!set.has(today)) d.setUTCDate(d.getUTCDate() - 1);
  let n = 0;
  while (set.has(d.toISOString().slice(0, 10))) { n++; d.setUTCDate(d.getUTCDate() - 1); }
  return n;
}

// ───────────────────────────── cheat sheet ─────────────────────────────
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

/** why a named pattern does not fit these prices (first broken rule) */
export function why(name, prices, abcd) {
  const pid = H_NAMES.indexOf(name);
  if (pid < 0) return "";
  const P = abcd ? [prices[0], ...prices] : prices;
  const ch = checks(pid, ...P.slice(0, 4), P[4] ?? null).list.filter((c) => !c.info && !c.ok);
  return ch.length ? `${name} needs ${ch.map((c) => `${c.leg} ${c.need} (here ${c.value})`).join(", ")}.` : "";
}

/** the cheat-sheet table for a drawing in progress */
export function sheetHtml(prices, abcd) {
  const rows = classify(prices, { abcd });
  const n = prices.length;
  const head = abcd ? ["Pattern", "C (BC/AB)", "D (CD/AB)"] : ["Pattern", "B (AB/XA)", "C", "D"];
  const val = (r, leg) => r.checks.find((c) => c.leg === leg);
  const cell = (r, leg, lo, hi, txt) => {
    const c = val(r, leg);
    const rng = hi >= 100 ? `≥${lo}` : lo === hi ? `${lo}` : `${lo}–${hi}`;
    if (!c) return `<td class="dim">${txt || rng}</td>`;
    return `<td class="${c.ok ? "ok" : "bad"}" title="${esc(c.text)}">${txt || rng}</td>`;
  };
  const list = abcd ? [ABCD] : [0, 1, 2, 3, 4, 5, 6, 7];
  const all = classify(prices, { abcd });
  const body = list.map((pid) => {
    const r = all.find((x) => x.pid === pid) || { checks: [], ok: false, name: H_NAMES[pid] };
    const s = SPEC[pid];
    const cells = abcd ? cell(r, "C", s[2], s[3]) + cell(r, "D", s[6], s[7])
      : cell(r, "B", s[0], s[1]) + cell(r, "C", s[2], s[3], pid === CYPHER ? `XC ${s[2]}–${s[3]}` : null) + cell(r, "D", s[6], s[7], pid === CYPHER ? `XC ${s[6]}` : null);
    const done = r.checks.length && r.ok;
    return `<tr class="${done ? "fit" : ""}"><td>${done ? "✓ " : ""}${esc(H_NAMES[pid])}</td>${cells}</tr>`;
  }).join("");
  const r0 = rows[0];
  const now = r0 ? (abcd ? ["C"] : ["B", "C", "D"]).map((leg) => { const c = r0.checks.find((x) => x.leg === leg); return c ? `${leg} ${c.value}` : null; }).filter(Boolean).join(" · ") : "";
  return `<div class="rs-head">📐 Ratios ${now ? `· yours: <b>${esc(now)}</b>` : `· place ${abcd ? "A, B, C" : "X, A, B"}…`}${r0 && !r0.zig && n >= (abcd ? 3 : 4) ? ` <span class="bad">not a zig-zag</span>` : ""}</div>
    <table><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr>${body}</table>
    <div class="rs-foot">green = your leg fits · red = it breaks the rule (±8%)</div>`;
}
