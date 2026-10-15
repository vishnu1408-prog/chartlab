// node --test frontend/tests
import test from "node:test";
import assert from "node:assert/strict";
import {
  candleCloseAt, fmtCountdown, mcxCloseMin,
  lastLE, timeToLogical, logicalToTime, ReplayModel, cursorForTimeframe, visibleAt, clipPoints, PaperEngine,
  fibPrices, xabcdRatios, rrLevels, hitTest, distToSegment, minuteOfDay, fmtTime, TOOL_POINTS,
} from "../js/core.js";

const T0 = Date.UTC(2026, 9, 7, 9, 15) / 1000;           // 07 Oct 2026 09:15 "IST as UTC"
const bar = (i, o, h, l, c, step = 300) => ({ time: T0 + i * step, open: o, high: h, low: l, close: c });

test("time helpers", () => {
  assert.equal(minuteOfDay(T0), 555);
  assert.equal(fmtTime(T0), "07 Oct 26 09:15");
  assert.equal(lastLE([1, 3, 5], 4), 1);
  assert.equal(lastLE([1, 3, 5], 0), -1);
  assert.equal(lastLE([1, 3, 5], 5), 2);
});

test("time ↔ logical index, inside and beyond the data", () => {
  const times = [0, 300, 600, 900];
  assert.equal(timeToLogical(times, 600, 300), 2);
  assert.equal(timeToLogical(times, 450, 300), 1.5);
  assert.equal(timeToLogical(times, 1500, 300), 5);           // future (projected D of a pattern)
  assert.equal(timeToLogical(times, -300, 300), -1);
  assert.equal(logicalToTime(times, 2.2, 300), 600);
  assert.equal(logicalToTime(times, 6, 300), 1800);
  // an overnight gap is one bar wide, not hundreds
  const gap = [0, 300, 86400, 86700];
  assert.ok(timeToLogical(gap, 43200, 300) > 1 && timeToLogical(gap, 43200, 300) < 2);
});

test("replay model: step, rewind, bounds, seek", () => {
  const bars = Array.from({ length: 10 }, (_, i) => bar(i, 1, 2, 0, 1));
  const r = new ReplayModel(bars, 3);
  assert.equal(r.visible().length, 4);
  r.step(1); assert.equal(r.cursor, 4);
  r.step(-2); assert.equal(r.cursor, 2);
  r.step(-50); assert.ok(r.atStart);
  r.step(50); assert.ok(r.atEnd); assert.equal(r.cursor, 9);
  r.seekTime(T0 + 3 * 300 + 10); assert.equal(r.cursor, 3);
});

test("switching timeframe in replay never reveals an unfinished higher-timeframe bar", () => {
  const t15 = [0, 900, 1800, 2700].map((x) => T0 + x);       // 15m bars 09:15 … 10:00
  // 5m cursor at the 09:40 bar → time known up to 09:45 → last COMPLETE 15m bar is 09:30 (ends 09:45)
  const end = T0 + 25 * 60 + 300;
  assert.equal(cursorForTimeframe(t15, end, 900), 1);
  // cursor at 09:35 bar (known to 09:40): 09:30 bar is not finished → 09:15 bar
  assert.equal(cursorForTimeframe(t15, T0 + 20 * 60 + 300, 900), 0);
});

test("indicator objects: visible_from, clipping, live-only", () => {
  const objs = [{ visible_from: 10 }, { visible_from: 20 }, { time: 5 }, { visible_from: 1, live_only: true }];
  assert.equal(visibleAt(objs, 15).length, 2);
  assert.equal(visibleAt(objs, null, false).length, 4);
  assert.deepEqual(clipPoints([{ time: 5, price: 1 }, { time: 50, price: 2 }], 20), [{ time: 5, price: 1 }, { time: 20, price: 2 }]);
});

test("paper: long stop and target", () => {
  const e = new PaperEngine();
  e.order(1, 65, 100, T0, { sl: 90, tp: 120 });
  e.lastBar = bar(0, 100, 100, 100, 100);
  assert.equal(e.onBar(bar(1, 101, 110, 95, 105)).length, 0);
  const [t] = e.onBar(bar(2, 105, 125, 100, 121));
  assert.equal(t.reason, "target"); assert.equal(t.exit, 120); assert.equal(t.pnl_rs, 20 * 65);
  e.order(1, 65, 100, bar(3).time, { sl: 90, tp: 120 });
  const [s] = e.onBar(bar(4, 99, 100, 85, 95));
  assert.equal(s.reason, "stop"); assert.equal(s.pnl_pts, -10);
  assert.equal(e.pos, null);
});

test("paper: stop beats target inside one bar, gaps fill at the open", () => {
  const e = new PaperEngine();
  e.order(1, 1, 100, T0, { sl: 95, tp: 105 });
  assert.equal(e.onBar(bar(1, 100, 110, 90, 100))[0].reason, "stop");
  e.order(-1, 1, 100, bar(2).time, { sl: 105, tp: 95 });
  const [g] = e.onBar(bar(3, 110, 112, 108, 111));             // gaps above the short's stop
  assert.equal(g.exit, 110); assert.equal(g.pnl_pts, -10);
});

test("paper: short P&L, averaging, reversal, costs", () => {
  const e = new PaperEngine({ costPerTrade: 40 });
  e.order(-1, 50, 200, T0);
  e.order(-1, 50, 210, T0 + 60);
  assert.equal(e.pos.entry, 205); assert.equal(e.pos.qty, 100);
  const out = e.order(1, 150, 195, T0 + 120);                   // close 100 short, open 50 long
  assert.equal(out[0].pnl_pts, 10); assert.equal(out[0].pnl_rs, 10 * 100 - 40);
  assert.equal(e.pos.side, 1); assert.equal(e.pos.qty, 50);
  assert.equal(e.unrealized(200), 250);
});

test("paper: intraday rules — square-off at 15:15, never overnight, no new trades after 15:15", () => {
  const e = new PaperEngine();
  e.order(1, 1, 100, T0);
  e.lastBar = bar(0, 100, 100, 100, 100);
  const sq = e.onBar({ time: T0 - 555 * 60 + 915 * 60, open: 103, high: 104, low: 102, close: 103 });
  assert.equal(sq[0].reason, "square-off 15:15"); assert.equal(sq[0].exit, 103);
  assert.throws(() => e.order(1, 1, 100, T0 - 555 * 60 + 920 * 60), /15:15/);
  e.order(1, 1, 100, T0 + 3600);
  e.lastBar = { time: T0 + 3600, open: 100, high: 100, low: 100, close: 101 };
  const nd = e.onBar(bar(0, 99, 99, 99, 99, 0) && { time: T0 + 86400, open: 99, high: 99, low: 99, close: 99 });
  assert.equal(nd[0].reason, "square-off (day end)"); assert.equal(nd[0].exit, 101);
});

test("paper: stats and drawdown", () => {
  const e = new PaperEngine();
  for (const [entry, exit] of [[100, 110], [100, 95], [100, 90], [100, 120]]) { e.order(1, 1, entry, T0); e.close(exit, T0 + 60); }
  const s = e.stats();
  assert.equal(s.trades, 4); assert.equal(s.wins, 2); assert.equal(s.net, 15); assert.equal(s.maxDD, -15);
  assert.throws(() => e.order(1, 0, 100, T0));
});

test("fibonacci, risk/reward and XABCD ratios", () => {
  const f = fibPrices(100, 200);
  assert.equal(f.find((x) => x.level === 0).price, 200);
  assert.equal(f.find((x) => x.level === 1).price, 100);
  assert.ok(Math.abs(f.find((x) => x.level === 0.618).price - 138.2) < 1e-9);
  const rr = rrLevels(100, 90, 2);
  assert.deepEqual([rr.target, rr.risk, rr.reward, rr.side], [120, 10, 20, 1]);
  assert.equal(rrLevels(100, 110, 3).target, 70);
  // the user's 07 Oct 2026 5-minute pattern
  const r = xabcdRatios([22675.0, 22552.6, 22606.8, 22546.6, 22648.8]);
  assert.ok(Math.abs(r.AB_XA - 0.443) < 0.001 && Math.abs(r.BC_AB - 1.111) < 0.001 && Math.abs(r.AD_XA - 0.786) < 0.001);
});

test("hit-testing drawings", () => {
  assert.equal(distToSegment(5, 5, 0, 0, 10, 0), 5);
  assert.deepEqual(hitTest("trend", [{ x: 0, y: 0 }, { x: 100, y: 100 }], 50, 52, 500), { part: "body" });
  assert.deepEqual(hitTest("trend", [{ x: 0, y: 0 }, { x: 100, y: 100 }], 100, 101, 500), { part: "handle", index: 1 });
  assert.equal(hitTest("trend", [{ x: 0, y: 0 }, { x: 100, y: 100 }], 80, 20, 500), null);
  assert.deepEqual(hitTest("hline", [{ x: 10, y: 50 }], 400, 53, 500), { part: "body" });
  assert.deepEqual(hitTest("ray", [{ x: 0, y: 0 }, { x: 10, y: 0 }], 450, 2, 500), { part: "body" });
  assert.deepEqual(hitTest("rect", [{ x: 10, y: 10 }, { x: 60, y: 60 }], 30, 30, 500), { part: "body" });
  assert.equal(TOOL_POINTS.xabcd, 5);
});

import { bucketStart, aggregate, spanBar, priceAt } from "../js/core.js";

test("minute replay: bucket start matches the backend's session-anchored bars", () => {
  const day = Date.UTC(2024, 5, 25) / 1000;
  const at = (h, m) => day + (h * 60 + m) * 60;
  assert.equal(bucketStart(at(9, 15), "15"), at(9, 15));
  assert.equal(bucketStart(at(9, 44), "15"), at(9, 30));
  assert.equal(bucketStart(at(15, 29), "60"), at(15, 15));     // NSE hourly: 09:15, 10:15 … 15:15
  assert.equal(bucketStart(at(10, 14), "60"), at(9, 15));
  assert.equal(bucketStart(at(13, 7), "D"), day);
  assert.equal(bucketStart(at(9, 17), "1"), at(9, 17));
});

test("minute replay: candles build up minute by minute", () => {
  const day = Date.UTC(2024, 5, 25) / 1000 + 555 * 60;
  const mins = [[100, 102, 99, 101], [101, 105, 100, 104], [104, 104, 97, 98], [98, 99, 96, 97], [97, 98, 95, 96], [96, 97, 94, 95]]
    .map(([o, h, l, c], i) => ({ time: day + i * 60, open: o, high: h, low: l, close: c, volume: 1 }));
  const b = aggregate(mins, "5", 2);                             // after 3 minutes: one forming 5m candle
  assert.equal(b.length, 1);
  assert.deepEqual([b[0].open, b[0].high, b[0].low, b[0].close], [100, 105, 97, 98]);
  const b2 = aggregate(mins, "5");                               // 6 minutes: closed 09:15 candle + new 09:20 candle
  assert.equal(b2.length, 2);
  assert.deepEqual([b2[0].close, b2[0].low, b2[1].open, b2[1].time - b2[0].time], [96, 95, 96, 300]);
});

test("replay visibility with knownUntil: a bar's objects appear only after it closes", () => {
  const T = 1000 * 900;
  const objs = [{ visible_from: T }, { visible_from: T + 900 }];
  assert.equal(visibleAt(objs, T + 600, true, 900).length, 0);   // 15m bar at T still forming (minute mode)
  assert.equal(visibleAt(objs, T + 900, true, 900).length, 1);   // closed
  assert.equal(visibleAt(objs, T + 1800, true, 900).length, 2);
});

test("option marking: premium bars over a span, last price lookup", () => {
  const bars = [0, 60, 120, 180].map((t, i) => ({ time: t, open: 100 + i, high: 103 + i, low: 98 + i, close: 101 + i }));
  assert.deepEqual(spanBar(bars, 60, 180), { time: 60, open: 101, high: 105, low: 99, close: 103 });
  assert.equal(spanBar(bars, 300, 360), null);
  assert.equal(priceAt(bars, 150), 103);
  assert.equal(priceAt(bars, -1), null);
});

import { PanelReplay } from "../js/replay.js";

test("split-screen replay: other windows follow the main window's clock, never ahead", () => {
  const day = Date.UTC(2024, 5, 4) / 1000 + 555 * 60;
  const mins = (n, base) => Array.from({ length: n }, (_, i) => ({ time: day + i * 60, open: base, high: base + 1, low: base - 1, close: base }));
  const main = new PanelReplay({}, "5", true, mins(60, 22000), []);
  const vix1m = new PanelReplay({}, "5", true, mins(60, 20).filter((_, i) => i !== 7), []);   // a missing minute
  const vix15 = new PanelReplay({}, "15", false, [0, 900, 1800, 2700].map((s) => ({ time: day + s, open: 20, high: 21, low: 19, close: 20 })), []);
  main.cursor = 9;                                           // 10 minutes revealed → known until 09:25
  const K = main.known;
  assert.equal(K, day + 600);
  vix1m.syncTo(K); vix15.syncTo(K);
  assert.equal(vix1m.knownAt(vix1m.cursor), K);              // same minute (index shifted by the gap)
  assert.equal(vix15.cursor, -1);                            // first 15m bar (09:15–09:30) not complete yet
  assert.equal(vix15.visibleBars().length, 0);
  main.cursor = 29; vix15.syncTo(main.known);                // known until 09:45
  assert.equal(vix15.cursor, 1);                             // 09:15 and 09:30 bars complete (09:30 bar ends 09:45)
  main.cursor = 5; vix1m.syncTo(main.known); vix15.syncTo(main.known);   // rewind
  assert.equal(vix1m.cursor, 5); assert.equal(vix15.cursor, -1);
  assert.equal(vix1m.visibleBars().length, 2);              // 6 minutes on a 5m chart: 09:15 closed + 09:20 forming
});

import { checkOrder, dayStats, DEFAULT_RULES } from "../js/rules.js";
import { analyse } from "../js/stats.js";
import { lineLevel, crossed } from "../js/alerts.js";

test("discipline rules block the right orders", () => {
  const day = Date.UTC(2024, 5, 25) / 1000, at = (h, m) => day + (h * 60 + m) * 60;
  const R = { ...DEFAULT_RULES };
  const ok = { qty: 65, sl: 10, isNewEntry: true, trades: [], pos: null };
  assert.equal(checkOrder(R, { ...ok, time: at(10, 0) }), null);
  assert.match(checkOrder(R, { ...ok, time: at(9, 16) }), /before 09:20/);
  assert.match(checkOrder(R, { ...ok, time: at(14, 45) }), /after 14:30/);
  assert.match(checkOrder(R, { ...ok, time: at(10, 0), sl: 0 }), /stop loss/);
  assert.match(checkOrder(R, { ...ok, time: at(10, 0), qty: 500 }), /quantity/);
  const tr = (h, m, pnl) => ({ entry_time: at(h, m), exit_time: at(h, m + 5), pnl_rs: pnl });
  assert.match(checkOrder(R, { ...ok, time: at(11, 0), trades: [tr(10, 0, 500), tr(10, 30, 300)] }), /max 2 trades/);
  assert.match(checkOrder(R, { ...ok, time: at(10, 10), trades: [tr(10, 0, -200)] }), /cool-down/);
  assert.equal(checkOrder(R, { ...ok, time: at(10, 25), trades: [tr(10, 0, -200)] }), null);      // 20 min later: fine
  assert.match(checkOrder({ ...R, maxTrades: 9 }, { ...ok, time: at(12, 0), trades: [tr(10, 0, -900), tr(11, 0, -700)] }), /daily loss|in a row/);
  assert.equal(checkOrder({ ...R, enabled: false }, { ...ok, time: at(9, 0), sl: 0 }), null);
  assert.equal(checkOrder(R, { ...ok, time: at(15, 0), isNewEntry: false }), null);                // exits never blocked
  assert.equal(dayStats([tr(10, 0, -100), tr(11, 0, -50)], null, at(12, 0)).consec, 2);
  assert.equal(dayStats([tr(10, 0, -100)], null, at(12, 0) + 86400).entries, 0);                     // new day resets
});

test("stats: KPIs and breakdowns", () => {
  const T = Date.UTC(2024, 5, 25, 10, 0) / 1000;
  const t = [{ entry_time: T, pnl_rs: 1000, tag: "Breakout" }, { entry_time: T + 3600, pnl_rs: -400, tag: "Breakout" },
             { entry_time: T + 86400, pnl_rs: -300, tag: "Harmonic", side: "BUY PE" }];
  const a = analyse(t);
  assert.equal(a.n, 3); assert.equal(a.net, 300); assert.equal(Math.round(a.winRate), 33);
  assert.equal(a.maxDD, -700); assert.equal(a.pf, 1000 / 700);
  assert.deepEqual(a.byTag.map((g) => [g.key, g.n, g.net]), [["Breakout", 2, 600], ["Harmonic", 1, -300]]);
  assert.deepEqual(a.byHour.map((g) => g.key), ["10:00", "11:00"]);
  assert.equal(a.bestDay, 600); assert.equal(a.worstDay, -300);
  assert.ok(a.byInstrument.some((g) => g.key === "BUY PE"));
  assert.equal(analyse([]).n, 0);
});

test("line alerts: level of horizontal / trend / ray lines and crossing", () => {
  const h = { type: "hline", points: [{ time: 0, price: 100 }] };
  const tr = { type: "trend", points: [{ time: 0, price: 100 }, { time: 100, price: 200 }] };
  const ray = { type: "ray", points: [{ time: 0, price: 100 }, { time: 100, price: 200 }] };
  assert.equal(lineLevel(h, 999), 100);
  assert.equal(lineLevel(tr, 50), 150); assert.equal(lineLevel(tr, 150), null);
  assert.equal(lineLevel(ray, 150), 250); assert.equal(lineLevel(ray, -10), null);
  assert.ok(crossed(100, 99, { high: 100.5, low: 98 }));
  assert.ok(crossed(100, 101, { high: 101, low: 99.9 }));
  assert.ok(!crossed(100, 99, { high: 99.9, low: 98 }));
  assert.ok(!crossed(null, 99, { high: 200, low: 0 }));
});

import { PATTERNS, isHigh, patternRatios, intersect } from "../js/core.js";

test("pattern tools: point counts match TradingView", () => {
  const want = { xabcd: 5, cypher: 5, hs: 7, abcd: 4, triangle: 4, threedrives: 6, ew_impulse: 6, ew_abc: 4, ew_triangle: 6, ew_wxy: 4, ew_wxyxz: 6, cyclic: 2, timecycles: 2, sine: 2 };
  for (const [k, n] of Object.entries(want)) { assert.equal(TOOL_POINTS[k], n, k); assert.ok(PATTERNS[k].name); }
  assert.deepEqual([...new Set(Object.values(PATTERNS).map((p) => p.group))], ["Chart patterns", "Elliott waves", "Cycles"]);
});

test("pattern labels go above highs and below lows", () => {
  const pr = [100, 120, 110, 130, 105];
  assert.deepEqual(pr.map((_, i) => isHigh(pr, i)), [false, true, false, true, false]);
});

test("pattern ratios: cypher and ABCD", () => {
  // Cypher: X 100, A 200 (XA 100), B 150 (AB/XA 0.5), C 230 (XC/XA 1.3), D 127.82 (CD/XC = 102.18/130 = 0.786)
  const cy = Object.fromEntries(patternRatios("cypher", [100, 200, 150, 230, 127.82]).map((r) => [`${r.i}-${r.j}`, r.value]));
  assert.ok(Math.abs(cy["0-2"] - 0.5) < 1e-9 && Math.abs(cy["0-3"] - 1.3) < 1e-9 && Math.abs(cy["2-4"] - 0.786) < 1e-3);
  const ab = patternRatios("abcd", [100, 200, 138.2, 238.2]);
  assert.ok(Math.abs(ab[0].value - 0.618) < 1e-9 && Math.abs(ab[1].value - 100 / 61.8) < 1e-9);
  assert.equal(patternRatios("hs", [1, 2, 3]).length, 0);
  assert.equal(patternRatios("xabcd", [100, 200, 138.2]).length, 1);    // while drawing: only complete legs
});

test("triangle apex and pattern hit-testing", () => {
  assert.deepEqual(intersect({ x: 0, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }, { x: 10, y: 0 }), { x: 5, y: 5 });
  assert.equal(intersect({ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 0, y: 5 }, { x: 10, y: 5 }), null);
  const hs = [0, 10, 20, 30, 40, 50, 60].map((x, i) => ({ x, y: i % 2 ? 0 : 50 }));
  assert.deepEqual(hitTest("hs", hs, 15, 25, 500), { part: "body" });
  assert.deepEqual(hitTest("cyclic", [{ x: 10, y: 50 }, { x: 40, y: 50 }], 70, 5, 500), { part: "body" });   // a repeated cycle line
  assert.equal(hitTest("cyclic", [{ x: 10, y: 50 }, { x: 40, y: 50 }], 55, 5, 500), null);
});

import { rangeProfile } from "../js/core.js";

test("fixed range volume profile: POC, value area, time-at-price fallback, range limits", () => {
  const bar = (time, o, h, l, c, volume = 0) => ({ time, open: o, high: h, low: l, close: c, volume });
  const bars = [bar(0, 100, 110, 100, 108, 10), bar(60, 108, 110, 104, 105, 50), bar(120, 105, 106, 104, 105, 200),
                bar(180, 105, 120, 105, 119, 10), bar(240, 119, 130, 118, 129, 999)];
  const P = rangeProfile(bars, 0, 180, 20);                     // the last bar (t=240) is outside the range
  assert.equal(P.bars, 4); assert.equal(P.mode, "volume");
  assert.equal(P.lo, 100); assert.equal(P.hi, 120);
  assert.ok(Math.abs(P.total - 270) < 1e-9);                   // every unit of volume lands in some row
  assert.ok(P.pocPrice > 104 && P.pocPrice < 106);              // the 200-volume bar sits at 104–106
  const inVA = P.rows.slice(P.vaLo, P.vaHi + 1).reduce((s, r) => s + r.vol, 0);
  assert.ok(inVA >= 0.7 * P.total && P.val <= P.pocPrice && P.vah >= P.pocPrice);
  const up = P.rows.reduce((s, r) => s + r.up, 0), down = P.rows.reduce((s, r) => s + r.down, 0);
  assert.ok(Math.abs(up - 220) < 1e-9 && Math.abs(down - 50) < 1e-9);   // up: 10 + 200 (close == open) + 10; down: 50
  const T = rangeProfile(bars.map((b) => ({ ...b, volume: 0 })), 0, 180, 20);
  assert.equal(T.mode, "time"); assert.ok(Math.abs(T.total - 4) < 1e-9);
  assert.equal(rangeProfile(bars, 500, 900), null);
  assert.deepEqual(rangeProfile(bars, 180, 0, 20).rows, P.rows);   // clicking right-to-left gives the same profile
});

test("fixed range volume profile in replay uses only revealed bars", () => {
  const bars = Array.from({ length: 10 }, (_, i) => ({ time: i * 60, open: 100 + i, high: 101 + i, low: 99 + i, close: 100.5 + i, volume: 1 }));
  const revealed = bars.slice(0, 6);                            // the chart only holds what replay has shown
  const P = rangeProfile(revealed, 0, 540, 10);
  assert.equal(P.bars, 6); assert.equal(P.hi, 106); assert.equal(P.last, 300);
});

import { haBar, heikinAshi } from "../js/core.js";

test("Heikin Ashi: formula, continuation and replay (no look-ahead)", () => {
  const bars = [{ time: 0, open: 100, high: 110, low: 95, close: 105 }, { time: 60, open: 105, high: 112, low: 104, close: 111 },
                { time: 120, open: 111, high: 111, low: 98, close: 99 }];
  const H = heikinAshi(bars);
  assert.equal(H[0].close, (100 + 110 + 95 + 105) / 4); assert.equal(H[0].open, (100 + 105) / 2);
  assert.equal(H[1].open, (H[0].open + H[0].close) / 2); assert.equal(H[1].close, (105 + 112 + 104 + 111) / 4);
  for (const h of H) { assert.ok(h.high >= Math.max(h.open, h.close) && h.low <= Math.min(h.open, h.close)); }
  // a forming bar rebuilt from the previous HA bar equals the full computation; earlier bars never change
  assert.deepEqual(haBar(bars[2], H[1]), H[2]);
  assert.deepEqual(heikinAshi(bars.slice(0, 2)), H.slice(0, 2));
});

test("24-hour markets: bars anchored at 05:30 IST (00:00 UTC), day labelled with its start date", () => {
  const day = Date.UTC(2026, 0, 6) / 1000, at = (h, m) => day + h * 3600 + m * 60;
  assert.equal(bucketStart(at(10, 40), "60", 330), at(10, 30));
  assert.equal(bucketStart(at(2, 10), "60", 330), at(1, 30));          // after midnight IST, still :30 bars
  assert.equal(bucketStart(at(2, 10), "D", 330), day - 86400);          // 02:10 IST belongs to the previous UTC day
  assert.equal(bucketStart(at(5, 30), "D", 330), day);
  assert.equal(bucketStart(at(23, 59), "15", 330), at(23, 45));
  const e = new PaperEngine();
  e.setSession(330);
  e.order(1, 1, 100, at(23, 0));                                       // 23:00 IST is fine for a 24-hour market
  assert.equal(e.onBar({ time: at(23, 5), open: 101, high: 101, low: 101, close: 101 }).length, 0);
  const d = e.onBar({ time: at(29, 15), open: 102, high: 102, low: 102, close: 102 });  // 05:15 IST next morning
  assert.equal(d[0].reason, "square-off 05:15");
});

test("MCX: day anchored at 09:00 IST, square-off at 23:00", () => {
  const day = Date.UTC(2026, 9, 12) / 1000, at = (h, m) => day + h * 3600 + m * 60;
  assert.equal(bucketStart(at(9, 7), "15", 540), at(9, 0));
  assert.equal(bucketStart(at(23, 20), "60", 540), at(23, 0));
  assert.equal(bucketStart(at(23, 20), "D", 540), day);
  const e = new PaperEngine();
  e.setSession(540, 23 * 60);
  assert.equal(e.sqLabel, "23:00");
  e.order(1, 10, 150000, at(20, 0));                                    // 20:00 is inside the MCX session
  assert.equal(e.onBar({ time: at(22, 55), open: 150010, high: 150010, low: 150010, close: 150010 }).length, 0);
  const d = e.onBar({ time: at(23, 0), open: 150020, high: 150020, low: 150020, close: 150020 });
  assert.equal(d[0].reason, "square-off 23:00");
  assert.throws(() => e.order(1, 10, 150000, at(23, 10)), /no new trades after 23:00/);
});

test("candle countdown: closes at the bar end, never past the session end", () => {
  const day = Date.UTC(2026, 9, 12) / 1000, at = (h, m) => day + h * 3600 + m * 60;
  assert.equal(candleCloseAt(at(10, 5), "5"), at(10, 10));
  assert.equal(candleCloseAt(at(10, 15), "60"), at(11, 15));
  assert.equal(candleCloseAt(at(15, 15), "60"), at(15, 30));                 // NSE last hourly candle: 15 minutes
  assert.equal(candleCloseAt(day, "D"), at(15, 30));                         // daily: at the session close
  assert.equal(candleCloseAt(at(23, 0), "60", 540, 1440), at(24, 0));       // MCX evening
  assert.equal(candleCloseAt(day, "D", 330, 1770), at(29, 30));             // 24-hour market: 05:30 next day
  assert.equal(candleCloseAt(at(2, 30), "60", 330, 1770), at(3, 30));       // after midnight IST, same trading day
  assert.equal(fmtCountdown(42), "00:42");
  assert.equal(fmtCountdown(245), "04:05");
  assert.equal(fmtCountdown(3807), "1:03:27");
  assert.equal(fmtCountdown(-5), "00:00");
});

test("MCX close follows US daylight saving: 23:30 in summer, 23:55 in winter", () => {
  const d = (y, m, dd) => Date.UTC(y, m - 1, dd) / 1000;
  assert.equal(mcxCloseMin(d(2026, 10, 12)), 1410);
  assert.equal(mcxCloseMin(d(2026, 11, 2)), 1435);          // US clocks went back on Sun 1 Nov 2026
  assert.equal(mcxCloseMin(d(2026, 1, 15)), 1435);
  assert.equal(mcxCloseMin(d(2026, 3, 9)), 1410);           // 2nd Sunday of March 2026 = 8 Mar
  assert.equal(mcxCloseMin(d(2026, 3, 6)), 1435);
  assert.equal(candleCloseAt(d(2026, 10, 12), "D", 540, mcxCloseMin), d(2026, 10, 12) + 1410 * 60);
});
