// Pure logic shared by the UI and the Node tests (no DOM, no chart library).
// Times are epoch seconds of IST wall-clock time (the backend treats IST as UTC so the chart axis shows IST).

export const TF_SECONDS = { "1": 60, "3": 180, "5": 300, "15": 900, "30": 1800, "60": 3600, "D": 86400 };
export const SQUARE_OFF_MIN = 15 * 60 + 15;   // 15:15 IST
export const NSE_ANCHOR = 555;                 // 09:15 IST: NSE session start (minutes of the day)
/** Session start of a symbol (minutes after 00:00 IST). 24-hour markets: 330 = 05:30 IST = 00:00 UTC. */
export const anchorOf = (sym) => sym?.session_start ?? NSE_ANCHOR;

export const minuteOfDay = (t) => Math.floor((((t % 86400) + 86400) % 86400) / 60);
export const dayOf = (t) => Math.floor(t / 86400);

const OPTION_ROOT = { "NIFTY 50": "NIFTY", "NIFTY BANK": "BANKNIFTY", "NIFTY FIN SERVICE": "FINNIFTY", "SENSEX": "SENSEX",
  "MCX CRUDEOIL": "CRUDEOIL", "MCX NATURALGAS": "NATURALGAS", "MCX GOLD": "GOLD", "MCX SILVER": "SILVER" };

/** the chart symbol of an option contract, e.g. "NIFTY 27-Oct-26 22950 CE" (the option's own premium chart) */
export function optionChartName({ symbol, expiry, strike, type }) {
  const d = new Date(expiry + "T00:00:00Z");
  const mon = d.toLocaleString("en-US", { month: "short", timeZone: "UTC" });
  return `${OPTION_ROOT[symbol] || symbol} ${String(d.getUTCDate()).padStart(2, "0")}-${mon}-${String(d.getUTCFullYear()).slice(2)} ${Number(strike)} ${type}`;
}

export function fmtTime(t, withDate = true) {
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  const hm = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
  if (!withDate) return hm;
  const mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()];
  return `${p(d.getUTCDate())} ${mon} ${String(d.getUTCFullYear()).slice(2)} ${hm}`;
}

export const fmtPrice = (p, dp = 2) => (p == null || !isFinite(p) ? "—" : Number(p).toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp }));

/** Index of the last element of sorted `arr` that is <= x (−1 if none). */
export function lastLE(arr, x) {
  let lo = 0, hi = arr.length - 1, ans = -1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1;
    if (arr[m] <= x) { ans = m; lo = m + 1; } else hi = m - 1;
  }
  return ans;
}

/** Map a time to a (fractional) bar index. Times beyond the data are extrapolated by the bar size. */
export function timeToLogical(times, t, tfSec) {
  const n = times.length;
  if (!n) return 0;
  if (t <= times[0]) return (t - times[0]) / tfSec;
  if (t >= times[n - 1]) return n - 1 + (t - times[n - 1]) / tfSec;
  const i = lastLE(times, t);
  const span = times[i + 1] - times[i];
  return i + Math.min(1, (t - times[i]) / Math.max(span, 1));
}

/** Inverse of timeToLogical for whole/fractional indices (snaps inside the data, extrapolates outside). */
export function logicalToTime(times, logical, tfSec) {
  const n = times.length;
  if (!n) return 0;
  if (logical <= 0) return times[0] + Math.round(logical) * tfSec;
  if (logical >= n - 1) return times[n - 1] + Math.round(logical - (n - 1)) * tfSec;
  return times[Math.round(logical)];
}

// ───────────────────────────── Replay ─────────────────────────────
export class ReplayModel {
  constructor(bars, cursor = 0) {
    this.bars = bars;
    this.times = bars.map((b) => b.time);
    this.cursor = Math.max(0, Math.min(cursor, bars.length - 1));
  }
  get current() { return this.bars[this.cursor]; }
  get time() { return this.current ? this.current.time : null; }
  get atEnd() { return this.cursor >= this.bars.length - 1; }
  get atStart() { return this.cursor <= 0; }
  step(n = 1) { this.cursor = Math.max(0, Math.min(this.bars.length - 1, this.cursor + n)); return this.cursor; }
  seekTime(t) { const i = lastLE(this.times, t); this.cursor = Math.max(0, i); return this.cursor; }
  visible() { return this.bars.slice(0, this.cursor + 1); }
  /** bar end time of the cursor bar (used when switching timeframe without leaking the future) */
  endTime(tfSec) { return this.time + tfSec; }
}

/** When switching timeframe in replay: last bar of the new timeframe that is COMPLETE by `endTime`. */
export function cursorForTimeframe(times, endTime, tfSec) {
  let i = lastLE(times, endTime - tfSec);
  return Math.max(0, i);
}

/** Objects from indicators carry visible_from = start time of the bar at whose CLOSE they became known.
 *  `knownUntil` = end of the data revealed so far (seconds); null = show everything (normal / live chart). */
export function visibleAt(objs, knownUntil, replay = true, tfSec = 0) {
  if (knownUntil == null) return objs.filter((o) => !(o.live_only && replay));
  return objs.filter((o) => (o.visible_from ?? o.time ?? 0) + tfSec <= knownUntil && !(replay && o.live_only));
}

/** Start time of the chart bar that contains time t, counted from the session start `anchor` (like the backend):
 *  NSE 09:15; 24-hour markets 05:30 IST, where a day runs 05:30 → 05:30 and is labelled with the date it starts on. */
export function bucketStart(t, tf, anchor = NSE_ANCHOR) {
  const sh = t - anchor * 60;
  const day = Math.floor(sh / 86400) * 86400;
  if (tf === "D") return day;
  const n = TF_SECONDS[tf] / 60;
  return day + (anchor + Math.floor(minuteOfDay(sh) / n) * n) * 60;
}

/** When the candle that starts at `t` closes (epoch s). A candle never runs past its session's end: NSE's last
 *  hourly bar (15:15) closes at 15:30; a daily bar (labelled 00:00 of its day) closes at the session end. */
export function candleCloseAt(t, tf, start = NSE_ANCHOR, end = 930) {
  const day = tf === "D" ? t : Math.floor((t - start * 60) / 86400) * 86400;
  const sessEnd = day + (typeof end === "function" ? end(day) : end) * 60;
  return tf === "D" ? sessEnd : Math.min(t + TF_SECONDS[tf], sessEnd);
}

/** MCX closes at 23:30 IST while the US is on summer time (2nd Sunday of March → 1st Sunday of November), else 23:55. */
export function mcxCloseMin(day) {
  const d = new Date(day * 1000), y = d.getUTCFullYear();
  const sunday = (m, n) => { const f = new Date(Date.UTC(y, m, 1)); return Date.UTC(y, m, 1 + ((7 - f.getUTCDay()) % 7) + 7 * (n - 1)) / 1000; };
  return day >= sunday(2, 2) && day < sunday(10, 1) ? 23 * 60 + 30 : 23 * 60 + 55;
}

/** Countdown label: "0:42", "4:05", "1:03:27" (hours only when needed, like TradingView). */
export function fmtCountdown(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60, p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}

/** Aggregate 1-minute bars into chart-timeframe bars (the last one may still be forming). */
export function aggregate(minutes, tf, upto = minutes.length - 1, anchor = NSE_ANCHOR) {
  const out = [];
  for (let i = 0; i <= upto && i < minutes.length; i++) {
    const m = minutes[i];
    const k = bucketStart(m.time, tf, anchor);
    const last = out[out.length - 1];
    if (last && last.time === k) {
      last.high = Math.max(last.high, m.high); last.low = Math.min(last.low, m.low); last.close = m.close; last.volume += m.volume || 0;
    } else out.push({ time: k, open: m.open, high: m.high, low: m.low, close: m.close, volume: m.volume || 0 });
  }
  return out;
}

/** Combine premium (or any) minute bars that fall inside [start, end) into one bar; null if none. */
export function spanBar(bars, start, end) {
  let b = null;
  for (const x of bars) {
    if (x.time < start) continue;
    if (x.time >= end) break;
    if (!b) b = { time: start, open: x.open, high: x.high, low: x.low, close: x.close };
    else { b.high = Math.max(b.high, x.high); b.low = Math.min(b.low, x.low); b.close = x.close; }
  }
  return b;
}

/** Last value at or before t from time-sorted bars. */
export function priceAt(bars, t) {
  const i = lastLE(bars.map((b) => b.time), t);
  return i >= 0 ? bars[i].close : null;
}

/** Heikin Ashi candles from real OHLC. HA close = (O+H+L+C)/4, HA open = (previous HA open + previous HA close)/2
 *  (first bar: (O+C)/2), HA high / low = extremes of the real high / low and the HA open / close. Each HA bar depends only
 *  on its own bar and the bars before it — safe in replay. Pass `prev` (the previous HA bar) to continue a series. */
export function haBar(b, prev) {
  const close = (b.open + b.high + b.low + b.close) / 4;
  const open = prev ? (prev.open + prev.close) / 2 : (b.open + b.close) / 2;
  return { time: b.time, open, high: Math.max(b.high, open, close), low: Math.min(b.low, open, close), close, volume: b.volume || 0 };
}

export function heikinAshi(bars) {
  const out = [];
  for (const b of bars) out.push(haBar(b, out[out.length - 1]));
  return out;
}

/** Fixed range volume profile over the bars whose time is in [t0, t1]. Each bar's volume is spread over the price rows
 *  its high–low covers (in proportion to the overlap). Bars with no volume at all in the range (indices) → every bar
 *  weighs 1: a time-at-price (TPO) profile. Up/down split by candle colour. Value area = rows around the POC holding
 *  `va` of the total, grown toward the bigger neighbour each step (the usual market-profile rule). */
export function rangeProfile(bars, t0, t1, rows = 24, va = 0.7) {
  const lo0 = Math.min(t0, t1), hi0 = Math.max(t0, t1);
  const sel = bars.filter((b) => b.time >= lo0 && b.time <= hi0);
  if (!sel.length) return null;
  const useVol = sel.some((b) => (b.volume || 0) > 0);
  const lo = Math.min(...sel.map((b) => b.low)), hi = Math.max(...sel.map((b) => b.high));
  const n = Math.max(1, Math.min(200, Math.round(rows)));
  const step = hi > lo ? (hi - lo) / n : 1;
  const R = Array.from({ length: n }, (_, i) => ({ lo: lo + i * step, hi: lo + (i + 1) * step, up: 0, down: 0, vol: 0 }));
  for (const b of sel) {
    const w = useVol ? b.volume || 0 : 1;
    if (!w) continue;
    const side = b.close >= b.open ? "up" : "down";
    const i0 = Math.min(n - 1, Math.floor((b.low - lo) / step)), i1 = Math.min(n - 1, Math.floor((b.high - lo) / step));
    const span = b.high - b.low;
    for (let i = i0; i <= i1; i++) {
      const part = span > 0 ? (Math.min(b.high, R[i].hi) - Math.max(b.low, R[i].lo)) / span : 1 / (i1 - i0 + 1);
      if (part > 0) { R[i][side] += w * part; R[i].vol += w * part; }
    }
  }
  const total = R.reduce((s, r) => s + r.vol, 0);
  let poc = 0;
  R.forEach((r, i) => { if (r.vol > R[poc].vol) poc = i; });
  let a = poc, z = poc, acc = R[poc].vol;
  while (acc < va * total && (a > 0 || z < n - 1)) {
    const dn = a > 0 ? R[a - 1].vol : -1, up = z < n - 1 ? R[z + 1].vol : -1;
    if (up >= dn) acc += R[++z].vol; else acc += R[--a].vol;
  }
  return { rows: R, lo, hi, step, poc, vaLo: a, vaHi: z, pocPrice: (R[poc].lo + R[poc].hi) / 2, vah: R[z].hi, val: R[a].lo,
           total, bars: sel.length, mode: useVol ? "volume" : "time", first: sel[0].time, last: sel[sel.length - 1].time };
}

/** Clamp shape points that lie after the cursor (e.g. a trade line that ends at a future exit). */
export function clipPoints(points, cursorTime) {
  return points.map((p) => (p.time > cursorTime ? { ...p, time: cursorTime } : p));
}

// ───────────────────────────── Practice (paper) trading ─────────────────────────────
export class PaperEngine {
  constructor({ costPerTrade = 0, squareOff = true } = {}) {
    this.costPerTrade = costPerTrade;   // ₹ per round trip (brokerage + taxes)
    this.squareOff = squareOff;         // intraday: auto-exit at 15:15 and never carry overnight
    this.setSession(NSE_ANCHOR);
    this.pos = null;                    // {side: 1|-1, qty, entry, time, sl, tp}
    this.trades = [];
    this.lastBar = null;
  }

  /** Square-off time follows the symbol: NSE 15:15 IST; 24-hour markets 15 minutes before the day ends (05:15 IST);
   *  `squareOff` (minutes of the IST day, e.g. MCX 23:00) overrides both. */
  setSession(anchor = NSE_ANCHOR, squareOff = 0) {
    this.anchor = anchor;
    this.sqAfter = squareOff ? (squareOff - anchor + 1440) % 1440
      : anchor === NSE_ANCHOR ? SQUARE_OFF_MIN - NSE_ANCHOR : 1440 - 15;   // minutes after session start
    const m = (anchor + this.sqAfter) % 1440;
    this.sqLabel = `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  }
  _late(t) { return minuteOfDay(t - this.anchor * 60) >= this.sqAfter; }
  _day(t) { return dayOf(t - this.anchor * 60); }

  /** Market order filled at `price`. Same side adds (average price); opposite side reduces / closes / reverses. */
  order(side, qty, price, time, { sl = null, tp = null } = {}) {
    if (!(qty > 0)) throw new Error("quantity must be > 0");
    if (this.squareOff && this._late(time)) throw new Error(`intraday: no new trades after ${this.sqLabel}`);
    const out = [];
    if (this.pos && this.pos.side !== side) {
      const closeQty = Math.min(qty, this.pos.qty);
      out.push(this._close(price, time, "manual", closeQty));
      qty -= closeQty;
    }
    if (qty > 0) {
      if (this.pos) {
        const q = this.pos.qty + qty;
        this.pos.entry = (this.pos.entry * this.pos.qty + price * qty) / q;
        this.pos.qty = q;
        if (sl != null) this.pos.sl = sl;
        if (tp != null) this.pos.tp = tp;
      } else {
        this.pos = { side, qty, entry: price, time, sl, tp };
      }
    }
    return out;
  }

  setBrackets(sl, tp) { if (this.pos) { this.pos.sl = sl; this.pos.tp = tp; } }

  close(price, time, reason = "manual") { return this.pos ? [this._close(price, time, reason, this.pos.qty)] : []; }

  _close(price, time, reason, qty) {
    const p = this.pos;
    const pts = (price - p.entry) * p.side;
    const cost = this.costPerTrade * (qty / p.qty);
    const tr = { side: p.side > 0 ? "BUY" : "SELL", qty, entry: p.entry, exit: price, entry_time: p.time, exit_time: time,
                 pnl_pts: pts, pnl_rs: pts * qty - cost, reason, sl: p.sl, tp: p.tp };
    this.trades.push(tr);
    p.qty -= qty;
    if (p.qty <= 0) this.pos = null;
    return tr;
  }

  /** Called for each NEW bar (replay step or live bar). Stop beats target inside one bar (conservative);
   *  a gap through the stop/target fills at the bar's open; intraday square-off at 15:15 and on a new day. */
  onBar(bar) {
    const done = [];
    const prev = this.lastBar;
    this.lastBar = bar;
    const p = this.pos;
    if (!p) return done;
    if (this.squareOff && prev && this._day(bar.time) !== this._day(prev.time)) {
      done.push(this._close(prev.close, prev.time, "square-off (day end)", p.qty));
      return done;
    }
    if (this.squareOff && this._late(bar.time)) {
      done.push(this._close(bar.open, bar.time, `square-off ${this.sqLabel}`, p.qty));
      return done;
    }
    const long = p.side > 0;
    if (p.sl != null) {
      const gap = long ? bar.open <= p.sl : bar.open >= p.sl;
      const hit = long ? bar.low <= p.sl : bar.high >= p.sl;
      if (gap || hit) { done.push(this._close(gap ? bar.open : p.sl, bar.time, "stop", p.qty)); return done; }
    }
    if (p.tp != null) {
      const gap = long ? bar.open >= p.tp : bar.open <= p.tp;
      const hit = long ? bar.high >= p.tp : bar.low <= p.tp;
      if (gap || hit) done.push(this._close(gap ? bar.open : p.tp, bar.time, "target", p.qty));
    }
    return done;
  }

  unrealized(price) { return this.pos ? (price - this.pos.entry) * this.pos.side * this.pos.qty : 0; }

  stats() {
    const t = this.trades;
    const net = t.reduce((a, x) => a + x.pnl_rs, 0);
    let eq = 0, peak = 0, dd = 0;
    for (const x of t) { eq += x.pnl_rs; peak = Math.max(peak, eq); dd = Math.min(dd, eq - peak); }
    const wins = t.filter((x) => x.pnl_rs > 0).length;
    return { trades: t.length, wins, winRate: t.length ? wins / t.length : 0, net, avg: t.length ? net / t.length : 0,
             maxDD: dd, pts: t.reduce((a, x) => a + x.pnl_pts, 0) };
  }
}

// ───────────────────────────── Drawing geometry ─────────────────────────────
export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.272, 1.618];
export const FIB_RET = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
export const FIB_EXT = [1.272, 1.618, 2.618];
// TradingView-like colours per level (line + band below it)
export const FIB_COLORS = { 0: "#787b86", 0.236: "#f23645", 0.382: "#ff9800", 0.5: "#4caf50", 0.618: "#089981",
  0.786: "#00bcd4", 1: "#787b86", 1.272: "#2962ff", 1.618: "#9c27b0", 2.618: "#e91e63" };
// Chart-pattern / Elliott / cycle tools (TradingView's "Patterns" menu). labels: one per click point (null = none).
export const PATTERNS = {
  xabcd:      { group: "Chart patterns", name: "XABCD pattern", labels: ["X", "A", "B", "C", "D"], fill: [[0, 1, 2], [2, 3, 4]] },
  cypher:     { group: "Chart patterns", name: "Cypher pattern", labels: ["X", "A", "B", "C", "D"], fill: [[0, 1, 2], [2, 3, 4]] },
  hs:         { group: "Chart patterns", name: "Head and shoulders", labels: [null, "Left Shoulder", null, "Head", null, "Right Shoulder", null],
                fill: [[0, 1, 2], [2, 3, 4], [4, 5, 6]] },
  abcd:       { group: "Chart patterns", name: "ABCD pattern", labels: ["A", "B", "C", "D"] },
  triangle:   { group: "Chart patterns", name: "Triangle pattern", labels: ["A", "B", "C", "D"] },
  threedrives:{ group: "Chart patterns", name: "Three drives pattern", labels: [null, "Drive 1", null, "Drive 2", null, "Drive 3"] },
  ew_impulse: { group: "Elliott waves", name: "Elliott impulse wave (1-2-3-4-5)", labels: ["0", "1", "2", "3", "4", "5"], wave: true },
  ew_abc:     { group: "Elliott waves", name: "Elliott correction wave (A-B-C)", labels: ["0", "A", "B", "C"], wave: true },
  ew_triangle:{ group: "Elliott waves", name: "Elliott triangle wave (A-B-C-D-E)", labels: ["0", "A", "B", "C", "D", "E"], wave: true },
  ew_wxy:     { group: "Elliott waves", name: "Elliott double combo wave (W-X-Y)", labels: ["0", "W", "X", "Y"], wave: true },
  ew_wxyxz:   { group: "Elliott waves", name: "Elliott triple combo wave (W-X-Y-X-Z)", labels: ["0", "W", "X", "Y", "X", "Z"], wave: true },
  cyclic:     { group: "Cycles", name: "Cyclic lines", labels: [null, null], cycle: true },
  timecycles: { group: "Cycles", name: "Time cycles", labels: [null, null], cycle: true },
  sine:       { group: "Cycles", name: "Sine line", labels: [null, null], cycle: true },
};

export const TOOL_POINTS = { pen: 2, trend: 2, ray: 2, hline: 1, vline: 1, rect: 2, fib: 2, fibext: 2, text: 1, measure: 2, rr: 2, frvp: 2,
  ...Object.fromEntries(Object.entries(PATTERNS).map(([k, v]) => [k, v.labels.length])) };

/** Is point i a swing high (label goes above) — compared with its neighbours. */
export function isHigh(prices, i) {
  const p = prices[i], a = prices[i - 1], b = prices[i + 1];
  if (a == null && b == null) return true;
  if (a == null) return p > b;
  if (b == null) return p > a;
  return p >= Math.max(a, b) || (p > Math.min(a, b) && p - Math.min(a, b) > Math.max(a, b) - p);
}

/** Fibonacci ratios shown on a pattern's dashed lines: [{i, j, value}] (index pairs of points). */
export function patternRatios(type, pr) {
  const r = (n, m) => (Math.abs(m) > 0 ? Math.abs(n) / Math.abs(m) : NaN);
  const L = (i, j) => pr[j] - pr[i];
  const out = (list) => list.filter(([i, j]) => pr[i] != null && pr[j] != null).map(([i, j, v]) => ({ i, j, value: v }));
  switch (type) {
    case "xabcd": return out([[0, 2, r(L(1, 2), L(0, 1))], [1, 3, r(L(2, 3), L(1, 2))], [2, 4, r(L(3, 4), L(2, 3))], [0, 4, r(L(1, 4), L(0, 1))]]);
    case "cypher": return out([[0, 2, r(L(1, 2), L(0, 1))], [0, 3, r(L(0, 3), L(0, 1))], [2, 4, r(L(3, 4), L(0, 3))]]);     // AB/XA, XC/XA, CD/XC
    case "abcd": return out([[0, 2, r(L(1, 2), L(0, 1))], [1, 3, r(L(2, 3), L(1, 2))]]);                                   // BC/AB, CD/BC
    case "threedrives": return out([[1, 3, r(L(2, 3), L(1, 2))], [3, 5, r(L(4, 5), L(3, 4))], [0, 2, r(L(1, 2), L(0, 1))], [2, 4, r(L(3, 4), L(2, 3))]]);
    default: return [];
  }
}

/** where two lines (p1→p2, p3→p4) meet (screen coordinates); null if parallel */
export function intersect(p1, p2, p3, p4) {
  const d = (p1.x - p2.x) * (p3.y - p4.y) - (p1.y - p2.y) * (p3.x - p4.x);
  if (Math.abs(d) < 1e-9) return null;
  const a = p1.x * p2.y - p1.y * p2.x, b = p3.x * p4.y - p3.y * p4.x;
  return { x: (a * (p3.x - p4.x) - (p1.x - p2.x) * b) / d, y: (a * (p3.y - p4.y) - (p1.y - p2.y) * b) / d };
}

/** Trend-based Fib extension (2 points, drag along the swing): 0 at the end of the drag, then the levels continue beyond it
 *  in the drag direction in steps of the swing size (0.5 = half the swing further, 1 = one full swing further, …). */
export const FIB_EXT_LEVELS = [0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4];
export function fibExtPrices(p0, p1, levels = FIB_EXT_LEVELS) {
  return levels.map((lv) => ({ level: lv, price: p1 + (p1 - p0) * lv }));
}
/** "0, 0.5, 1" → sorted unique numbers (null if nothing valid) */
export function parseLevels(text) {
  const v = [...new Set(String(text || "").split(/[,\s]+/).filter((s) => s !== "").map(Number).filter(Number.isFinite))].sort((a, b) => a - b);
  return v.length ? v : null;
}

export function fibPrices(p0, p1, levels = FIB_LEVELS) {
  // 0 at the second point (end of the move), 1 at the start — TradingView convention for retracements
  return levels.map((lv) => ({ level: lv, price: p1 - (p1 - p0) * lv }));
}

/** Harmonic ratios of five points X A B C D (prices). */
export function xabcdRatios([x, a, b, c, d]) {
  const xa = Math.abs(a - x), ab = Math.abs(b - a), bc = Math.abs(c - b), cd = Math.abs(d - c);
  const r = (n, m) => (m > 0 ? n / m : NaN);
  return { AB_XA: r(ab, xa), BC_AB: r(bc, ab), CD_BC: r(cd, bc), AD_XA: r(Math.abs(d - a), xa) };
}

/** Risk/reward tool: entry at p0, stop at p1, target at `ratio` × risk on the other side. */
export function rrLevels(entry, stop, ratio = 2) {
  const risk = entry - stop;
  return { entry, stop, target: entry + ratio * risk, risk: Math.abs(risk), reward: Math.abs(ratio * risk), side: risk > 0 ? 1 : -1 };
}

export function distToSegment(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1;
  const len2 = dx * dx + dy * dy;
  let t = len2 ? ((px - x1) * dx + (py - y1) * dy) / len2 : 0;
  t = Math.max(0, Math.min(1, t));
  const cx = x1 + t * dx, cy = y1 + t * dy;
  return Math.hypot(px - cx, py - cy);
}

/** Hit-test a drawing in screen coordinates. `pts` = projected anchor points [{x,y}], `w` = pane width.
 *  Returns {part: "handle", index} | {part: "body"} | null. */
export function hitTest(type, pts, px, py, w, tol = 6) {
  if (type === "pen") {                                      // a freehand stroke moves as a whole (no point handles)
    for (let i = 0; i + 1 < pts.length; i++) if (distToSegment(px, py, pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y) <= tol) return { part: "body" };
    return null;
  }
  for (let i = 0; i < pts.length; i++) if (Math.hypot(px - pts[i].x, py - pts[i].y) <= tol + 2) return { part: "handle", index: i };
  const near = (x1, y1, x2, y2) => distToSegment(px, py, x1, y1, x2, y2) <= tol;
  const [a, b] = pts;
  switch (type) {
    case "hline": return Math.abs(py - a.y) <= tol ? { part: "body" } : null;
    case "vline": return Math.abs(px - a.x) <= tol ? { part: "body" } : null;
    case "trend": case "measure": return near(a.x, a.y, b.x, b.y) ? { part: "body" } : null;
    case "ray": {
      const k = b.x !== a.x ? (b.y - a.y) / (b.x - a.x) : 0;
      const ex = b.x >= a.x ? w : 0;
      return near(a.x, a.y, ex, a.y + k * (ex - a.x)) ? { part: "body" } : null;
    }
    case "fib": {
      // levels run from the end of the drag (0) past its start (1) up to 4× the swing — hit anywhere in that band
      if (near(a.x, a.y, b.x, b.y)) return { part: "body" };
      const y4 = b.y + (a.y - b.y) * 4, x1 = Math.min(a.x, b.x);
      return px >= x1 - tol && px <= w + tol && py >= Math.min(b.y, y4) - tol && py <= Math.max(b.y, y4) + tol ? { part: "body" } : null;
    }
    case "rect": case "rr": case "frvp": {
      const box = type === "rect" || type === "frvp";
      const x1 = Math.min(a.x, b.x), x2 = box ? Math.max(a.x, b.x) : Math.max(a.x, b.x, a.x + 1);
      const y1 = Math.min(a.y, b.y), y2 = Math.max(a.y, b.y);
      const inside = px >= x1 - tol && px <= (box ? x2 : w) + tol && py >= y1 - tol && py <= y2 + tol;
      return inside ? { part: "body" } : null;
    }
    case "fibext": {
      // the dashed swing, or anywhere in the band of levels to the right of its end (levels are linear in pixels)
      if (near(a.x, a.y, b.x, b.y)) return { part: "body" };
      const y4 = b.y + (b.y - a.y) * 4;
      return px >= b.x - tol && px <= w + tol && py >= Math.min(b.y, y4) - tol && py <= Math.max(b.y, y4) + tol ? { part: "body" } : null;
    }
    case "text": return Math.abs(px - a.x) < 40 && Math.abs(py - a.y) < 12 ? { part: "body" } : null;
    case "cyclic": case "timecycles": case "sine": {
      if (near(a.x, a.y, b.x, b.y)) return { part: "body" };
      const step = Math.abs(b.x - a.x);
      if (type === "cyclic" && step > 2) for (let x = Math.min(a.x, b.x); x <= w; x += step) if (Math.abs(px - x) <= tol) return { part: "body" };
      return null;
    }
    default:
      if (PATTERNS[type]) {                                   // every multi-point pattern: hit on any leg
        for (let i = 0; i + 1 < pts.length; i++) if (near(pts[i].x, pts[i].y, pts[i + 1].x, pts[i + 1].y)) return { part: "body" };
      }
      return null;
  }
}
