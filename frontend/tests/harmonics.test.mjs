import test from "node:test";
import assert from "node:assert/strict";
import { prz, checks, classify, dZones, zigzag, snapToSwings, gradeAgainst, kindsOf, simulatePlan, planError, practiceStats,
         mistakeHints, drillStreak, sheetHtml, why, H_NAMES } from "../js/harmonics.js";

const BAT = { x: 100, a: 200, b: 155 };
BAT.c = BAT.b + 0.618 * (BAT.a - BAT.b);
BAT.d = BAT.a - 0.886 * 100;

test("harmonic ratios match the backend rules (textbook Bat)", () => {
  const { x, a, b, c, d } = BAT;
  const ch = checks(1, x, a, b, c, d);
  assert.ok(ch.ok);
  assert.equal(ch.list[0].text, "B retraced 45.0% of XA"); assert.equal(ch.list[0].need, "0.382–0.500");
  assert.ok(!checks(0, x, a, b, c, d).ok);                                   // B 0.45 is not a Gartley
  const z = prz(1, x, a, b, c);
  assert.ok(z.ok && Math.abs(z.lo - (200 - 0.886 * 1.08 * 100)) < 1e-9 && Math.abs(z.hi - (200 - 0.886 * 0.92 * 100)) < 1e-9);
  const fits = classify([x, a, b, c, d]).filter((r) => r.ok).map((r) => r.name);
  assert.deepEqual(fits, ["Bat"]);
  assert.ok(dZones([x, a, b, c]).some((zz) => zz.name === "Bat"));
  assert.deepEqual(dZones([100, 200, 250, 260]), []);                        // not a zig-zag: no zones
  assert.match(why("Gartley", [x, a, b, c, d], false), /Gartley needs B 0\.618/);
  const ab = classify([200, 150, 180, 130], { abcd: true });                 // AB=CD: BC/AB 0.6, CD = AB
  assert.equal(ab.length, 1); assert.ok(ab[0].ok);
});

test("cheat sheet marks fitting rows and broken legs", () => {
  const { x, a, b, c, d } = BAT;
  const html = sheetHtml([x, a, b, c, d], false);
  assert.match(html, /<tr class="fit"><td>✓ Bat<\/td>/);
  assert.ok(!/<tr class="fit"><td>✓ Gartley/.test(html));
  assert.match(html, /class="bad"/);
  assert.match(sheetHtml([x, a], false), /place X, A, B/);
});

// a zig-zag series of bars: swing high at 10, low at 20, high at 30, low at 40
function swings() {
  const bars = [], path = [[0, 100], [10, 140], [20, 110], [30, 135], [40, 105], [50, 125]];
  for (let k = 0; k + 1 < path.length; k++) {
    const [i0, p0] = path[k], [i1, p1] = path[k + 1];
    for (let i = i0; i < i1; i++) {
      const m = p0 + ((p1 - p0) * (i - i0)) / (i1 - i0);
      bars.push({ time: 1700000000 + i * 300, open: m, high: m + 1, low: m - 1, close: m });
    }
  }
  return bars;
}

test("zigzag: alternating swings, confirmed L bars later", () => {
  const Z = zigzag(swings(), 3);
  assert.deepEqual(Z.map((s) => s.i), [10, 20, 30, 40]);
  assert.deepEqual(Z.map((s) => s.dir), [1, -1, 1, -1]);
  assert.ok(Z.every((s) => s.conf === s.i + 3));
});

test("check my drawing: early / late / exact / missing points", () => {
  const bars = swings(), T = (i) => bars[i].time;
  const pts = [{ time: T(10), price: 141 }, { time: T(22), price: 112 }, { time: T(29), price: 133 }, { time: T(40), price: 104 }];
  const kinds = kindsOf(pts.map((p) => p.price));
  assert.deepEqual(kinds, [1, -1, 1, -1]);
  const r = snapToSwings(pts, kinds, bars);
  assert.ok(r[0].exact);
  assert.equal(r[1].barsOff, 2); assert.equal(r[1].real.price, 109);       // 2 bars late, real low 110 − 1
  assert.equal(r[2].barsOff, -1);                                             // 1 bar early
  assert.ok(r[3].exact);
  const far = snapToSwings([{ time: T(0), price: 101 }], [1], bars);        // no swing high within 8 bars of bar 0
  assert.equal(far[0].real, null);
  const g = gradeAgainst([{ time: T(11), price: 140 }], [{ time: T(10), price: 141 }], bars);
  assert.equal(g[0].barsOff, 1); assert.equal(g[0].ptsOff, -1);
});

const day = Date.UTC(2026, 9, 5) / 1000;
const bar = (hhmm, o, h, l, c) => ({ time: day + (Math.floor(hhmm / 100) * 60 + (hhmm % 100)) * 60, open: o, high: h, low: l, close: c });

test("trade plan: limit fill, target, stop-first, cancel, intraday square-off", () => {
  const from = day + 10 * 3600;
  const plan = { side: 1, entry: 100, stop: 95, target: 110, from, cancel: 120 };
  const win = simulatePlan([bar(1005, 105, 106, 104, 105), bar(1010, 104, 104, 99, 101), bar(1015, 101, 111, 100, 110)], plan);
  assert.equal(win.status, "done"); assert.equal(win.reason, "target"); assert.equal(win.fill.price, 100); assert.equal(win.R, 2);
  const both = simulatePlan([bar(1010, 101, 101, 94, 96)], plan);           // fill and stop in one bar → stop
  assert.equal(both.reason, "stop (same bar)"); assert.equal(both.R, -1);
  const gap = simulatePlan([bar(1010, 98, 99, 97, 98), bar(1015, 93, 94, 92, 93)], plan);
  assert.equal(gap.fill.price, 98); assert.equal(gap.exit.price, 93);       // gap fill at the open, gap stop at the open
  assert.equal(simulatePlan([bar(1010, 110, 121, 109, 120)], plan).status, "cancelled");
  const late = simulatePlan([bar(1515, 101, 101, 99, 100), bar(1520, 100, 100, 98, 99)], plan);
  assert.equal(late.status, "waiting");                                      // no new entries from 15:15
  const sq = simulatePlan([bar(1500, 101, 101, 99, 100), bar(1505, 100, 103, 99, 102), bar(1515, 102, 104, 101, 103)], plan);
  assert.equal(sq.reason, "square-off 15:15"); assert.equal(sq.exit.price, 102);
  const open = simulatePlan([bar(1010, 101, 101, 99, 100)], plan);
  assert.equal(open.status, "open");
  assert.equal(simulatePlan([bar(1010, 105, 106, 104, 105), bar(1015, 105, 106, 104, 105)], { ...plan, maxBars: 1 }).status, "expired");
  const short = simulatePlan([bar(1010, 99, 101, 98, 99), bar(1015, 99, 99, 89, 90)], { side: -1, entry: 100, stop: 105, target: 90, from, cancel: 80 });
  assert.equal(short.reason, "target"); assert.equal(short.R, 2);
  assert.equal(planError(1, 100, 95, 110), null); assert.ok(planError(1, 100, 105, 110)); assert.ok(planError(-1, 100, 95, 90));
});

test("practice stats, habit hints and drill streak", () => {
  const log = [
    { mode: "quiz", pattern: "Bat", answer: "Gartley", ok: false, at: "2026-10-10T10:00:00" },
    { mode: "quiz", pattern: "Bat", answer: "Gartley", ok: false, at: "2026-10-10T10:01:00" },
    { mode: "quiz", pattern: "Crab", answer: "Crab", ok: true, at: "2026-10-11T10:00:00" },
    ...[0, 1, 2].map(() => ({ mode: "check", ok: false, at: "2026-10-11T11:00:00", points: [{ label: "B", barsOff: 2, ptsOff: 3, exact: false }, { label: "X", barsOff: null }] })),
    { mode: "plan", status: "done", R: -1, ok: false }, { mode: "plan", status: "done", R: 2, ok: true }, { mode: "plan", status: "expired", R: 0 },
  ];
  const S = practiceStats(log);
  assert.equal(S.modes.quiz.n, 3); assert.equal(S.modes.quiz.ok, 1);
  assert.equal(S.patterns.Bat.n, 2);
  assert.equal(S.points.B.late, 3); assert.equal(S.points.X.missed, 3);
  assert.deepEqual(S.plan, { n: 3, wins: 1, R: 1, filled: 2 });
  assert.equal(S.days["2026-10-11"].n, 4);
  const h = mistakeHints(S).join(" | ");
  assert.match(h, /mark B too late/); assert.match(h, /put X where there is no real swing/); assert.match(h, /Bat → you said Gartley \(2×\)/);
  assert.equal(drillStreak(["2026-10-09", "2026-10-10", "2026-10-11"], "2026-10-11"), 3);
  assert.equal(drillStreak(["2026-10-09", "2026-10-10"], "2026-10-11"), 2);    // today not done yet: streak still alive
  assert.equal(drillStreak(["2026-10-08"], "2026-10-11"), 0);
  assert.ok(H_NAMES.includes("Cypher"));
});
