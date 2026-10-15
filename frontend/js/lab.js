// 🎓 Harmonic practice lab — tools that give feedback on YOUR work (the coach only shows its own):
//  1 ✔ Check my drawing   grade a drawn XABCD against the real swing points + which pattern the ratios really make
//  2 📝 Trade-plan drill  replay stops at C; you place entry / stop / target; replay plays on and scores it in R
//  3 📈 Practice stats    accuracy per pattern, per point (early / late / missed), confusions, plan R, progress by day
//  4 D-zone preview       while drawing XABCD the chart shades where D must land for each pattern (drawings.zoneHook)
//  5 Ratio cheat sheet    live table beside the chart; the rows your drawing fits light up, broken rules turn red
//  6 🃏 Name that pattern flashcards from real NIFTY history, 30 s timer, streak
//  7 📚 Pattern library   every real pattern in a date range, filter by type / outcome; open one in replay at C
//  8 📅 Daily drill       10 rounds a day mixing name / mark / plan, with a day streak
// Everything in replay uses only bars already revealed; trade plans follow the intraday rules (no fills from 15:15,
// open positions closed at 15:15).
import { api, store } from "./api.js";
import { fmtPrice, fmtTime, TF_SECONDS, PATTERNS, patternRatios, isHigh } from "./core.js";
import { esc } from "./indicators.js";
import { badge, label } from "./chart.js";
import { H_NAMES, SPEC, ABCD, CYPHER, classify, dZones, snapToSwings, gradeAgainst, kindsOf, simulatePlan, planError,
         practiceStats, mistakeHints, drillStreak, why, sheetHtml } from "./harmonics.js";

const get = async (url) => { const r = await fetch(url); const d = await r.json(); if (!r.ok) throw new Error(d.detail || r.statusText); return d; };
const q = (o) => Object.entries(o).filter(([, v]) => v != null && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
const $ = (s) => document.querySelector(s);
const HARM_TOOLS = new Set(["xabcd", "cypher", "abcd"]);
const REAL = "#f5a623", YOU = "#e040fb", BULL = "#26a69a", BEAR = "#ef5350", LOT = 65;
const LABELS5 = ["X", "A", "B", "C", "D"], LABELS4 = ["A", "B", "C", "D"];
const today = () => new Date(Date.now() + 5.5 * 3600e3).toISOString().slice(0, 10);       // IST date
const seeded = (seed) => () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };

export class HarmonicLab {
  constructor(app) {
    this.app = app;
    this.task = null;              // the running exercise: {kind, panel, ...}
    this.sheetOn = store.get("ratioSheet", true);
    this.przOn = store.get("przPreview", true);
    this.streak = store.get("quizStreak", { now: 0, best: 0 });
    this.el = $("#lab");
    this.sheet = $("#ratio-sheet");
    this._sheetKey = null;
    this.el.querySelector(".lb-close").onclick = () => this.exitTask();
    this.buildMenu();
  }

  // ── wiring ──
  attach(panel) {
    panel.view.layers.push((ctx) => this.draw(ctx, panel));
    panel.view.chart.subscribeClick((p) => this.onChartClick(panel, p));
    panel.drawings.zoneHook = (ctx, it, pts) => this.drawZones(ctx, panel, it, pts);
  }

  buildMenu() {
    const m = $("#lab-menu"), btn = $("#btn-lab");
    m.innerHTML = `<div class="pm-head">Grade your work</div>
      <button data-a="check">✔ Check my drawing <span class="pm-n">draw XABCD first</span></button>
      <button data-a="plan">📝 Trade-plan drill <span class="pm-n">replay</span></button>
      <div class="pm-head">Learn faster</div>
      <button data-a="sheet">📐 Ratio cheat sheet <span class="pm-n lb-sheet-st"></span></button>
      <button data-a="prz">🎯 D-zone preview while drawing <span class="pm-n lb-prz-st"></span></button>
      <button data-a="quiz">🃏 Name that pattern <span class="pm-n">flashcards</span></button>
      <div class="pm-head">Real examples</div>
      <button data-a="library">📚 Pattern library <span class="pm-n">worked & failed</span></button>
      <button data-a="drill">📅 Daily drill <span class="pm-n">10 rounds</span></button>
      <button data-a="stats">📈 My practice stats</button>`;
    const mark = () => {
      m.querySelector(".lb-sheet-st").textContent = this.sheetOn ? "on" : "off";
      m.querySelector(".lb-prz-st").textContent = this.przOn ? "on" : "off";
    };
    mark();
    btn.onclick = (e) => {
      e.stopPropagation();
      if (!m.hidden) { m.hidden = true; return; }
      const r = btn.getBoundingClientRect();
      m.style.top = `${r.bottom + 4}px`; m.style.left = `${Math.min(r.left, window.innerWidth - 300)}px`;
      mark(); m.hidden = false;
    };
    document.addEventListener("mousedown", (e) => { if (!m.hidden && !m.contains(e.target) && e.target !== btn) m.hidden = true; });
    const act = {
      check: () => {
        const p = this.app.active, it = p.drawings.selected;
        if (it && HARM_TOOLS.has(it.type)) return this.check(p, it);
        const last = [...p.drawings.items].reverse().find((x) => HARM_TOOLS.has(x.type));
        if (last) return this.check(p, last);
        this.app.toast("Draw an XABCD (⋀⋁ menu → XABCD pattern) on the chart, then choose ✔ Check — or press ✔ in the drawing's box");
      },
      plan: () => this.startPlan(),
      sheet: () => { this.sheetOn = !this.sheetOn; store.set("ratioSheet", this.sheetOn); this._sheetKey = null; this.app.panels.forEach((p) => p.view.requestDraw()); if (!this.sheetOn) this.sheet.hidden = true; this.app.toast(this.sheetOn ? "📐 Cheat sheet on: select or draw an XABCD to see it" : "📐 Cheat sheet off"); },
      prz: () => { this.przOn = !this.przOn; store.set("przPreview", this.przOn); this.app.panels.forEach((p) => p.view.requestDraw()); this.app.toast(this.przOn ? "🎯 D-zone preview on: place X, A, B, C and the zones appear" : "🎯 D-zone preview off"); },
      quiz: () => this.startQuiz(),
      library: () => this.openLibrary(),
      drill: () => this.startDrill(),
      stats: () => this.openStats(),
    };
    m.querySelectorAll("button[data-a]").forEach((b) => (b.onclick = () => { m.hidden = true; act[b.dataset.a](); }));
  }

  async log(entry) {
    const p = this.task?.panel || this.app.active;
    const at = new Date(Date.now() + 5.5 * 3600e3).toISOString().slice(0, 19);                  // IST, so days match the drill
    const e = { at, symbol: p?.state.symbol, tf: p?.state.tf, ...entry, drill: this.drill ? this.drill.date : entry.drill };
    try { await fetch("/api/practice", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(e) }); }
    catch { /* offline: stats just miss this one */ }
  }

  // ── card ──
  card(title, html) {
    this.el.hidden = false;
    if (this.app.coach.on || this.app.coach.ch) { if (this.app.coach.ch) this.app.coach.endChallenge(); this.app.coach.toggle(false); }
    const d = this.drill;
    this.el.querySelector(".lb-title").textContent = title;
    this.el.querySelector(".lb-drill").innerHTML = d ? `📅 Daily drill <b>${Math.min(d.idx + 1, d.tasks.length)}/${d.tasks.length}</b> · ${d.results.filter((r) => r.ok).length} correct so far` : "";
    this.el.querySelector(".lb-body").innerHTML = html;
    return this.el.querySelector(".lb-body");
  }

  /** leave the current exercise: back to live data, remove its overlays and the drawings it created */
  async exitTask(keepDrill = false) {
    const t = this.task;
    this.task = null;
    clearInterval(this._timer);
    if (!keepDrill) this.drill = null;
    this.el.hidden = true;
    if (t) {
      if (t.drawIds) {
        const p = t.panel, keep = p.drawings.items.filter((x) => !t.drawIds.has(x.id) || t.keepDrawings);
        if (keep.length !== p.drawings.items.length) { p.drawings.setItems(keep); this.app.saveDrawings(p, keep); }
      }
      if (t.replay && this.app.replay.active) this.app.replay.exit();
      else if (t.window) await t.panel.load();
    }
    this.app.panels.forEach((p) => p.view.requestDraw());
  }

  /** show a fixed window of history (no replay) — used by flashcards and the drill's mark task */
  async showWindow(panel, item, { before = 40, after = 10, hideIndicators = true } = {}) {
    const { symbol, tf } = panel.state, sec = TF_SECONDS[tf];
    const x = item.points[0].time, dT = item.points[item.points.length - 1].time;
    const [h, f] = await Promise.all([api.candles({ symbol, tf, before: x, limit: before }),
                                      api.candles({ symbol, tf, start: x, end: dT + 10 * 86400, limit: 5000 })]);
    const di = f.bars.findIndex((b) => b.time >= dT);
    const bars = h.bars.concat(f.bars.slice(0, di < 0 ? f.bars.length : di + 1 + after));
    if (this.app.replay.active) this.app.replay.exit(false);
    panel.closeLive();
    panel.view.knownUntil = null;
    if (hideIndicators) panel.indicators.clearAll();             // the harmonic indicator would give the answer away
    panel.view.setBars(bars, tf);
    panel.view.autoScale();
    // zoom on the pattern (a few bars around X…D) so its legs are big enough to read; the rest stays scrollable
    const xi = bars.findIndex((b) => b.time >= x), last = bars.length - 1;
    try { panel.view.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, xi - 12), to: last + 6 }); } catch { /* ignore */ }
    panel.ohlcLegend({});
    return bars;
  }

  /** show from a few bars before `time` to `ahead` empty bars after the last revealed bar */
  zoomTo(panel, time, ahead) {
    const bars = panel.view.bars, i = bars.findIndex((b) => b.time >= time);
    try { panel.view.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, i - 12), to: bars.length - 1 + ahead }); } catch { /* ignore */ }
  }

  /** a random real pattern from the library (random 120-day slice of the data) */
  async randomItem(panel, filter = () => true) {
    const st = panel.state;
    const first = st.first || Date.now() / 1000 - 400 * 86400, last = st.last || Date.now() / 1000;
    for (let tries = 0; tries < 6; tries++) {
      const e = first + 150 * 86400 + Math.random() * Math.max(86400, last - first - 150 * 86400);
      const day = (s) => new Date(s * 1000).toISOString().slice(0, 10);
      try {
        const r = await get(`/api/harmonics/library?${q({ symbol: st.symbol, tf: st.tf, start: day(e - 120 * 86400), end: day(e), limit: 400 })}`);
        const ok = r.items.filter(filter);
        if (ok.length) return ok[Math.floor(Math.random() * ok.length)];
      } catch { /* try another slice */ }
    }
    return null;
  }

  // ═════════════════════════ 1 · Check my drawing ═════════════════════════
  check(panel, it, target = null) {
    const abcd = it.type === "abcd";
    const labels = abcd ? LABELS4 : LABELS5;
    if (it.points.length < labels.length) { this.app.toast("Finish the drawing first (all points)"); return; }
    const pts = it.points.slice(0, labels.length), prices = pts.map((p) => p.price);
    const kinds = kindsOf(prices);
    const bars = panel.view.bars;
    const res = target ? gradeAgainst(pts, target.points, bars) : snapToSwings(pts, kinds, bars);
    const realPrices = res.map((r, i) => (r.real ? r.real.price : prices[i]));
    const yours = classify(prices, { abcd }).filter((x) => x.ok).map((x) => x.name);
    const real = target ? target.patterns : classify(realPrices, { abcd }).filter((x) => x.ok).map((x) => x.name);
    const exact = res.filter((r) => r.exact).length;
    let t = this.task && this.task.panel === panel ? this.task : { kind: "check", panel };
    t.checked = { it, res, labels, yours, real, realPrices, prices, abcd };
    this.task = t;
    const rows = res.map((r, i) => {
      const lb = labels[i], kind = kinds[i] === 1 ? "high" : "low";
      if (!r.real) return `<li class="down">✗ <b>${lb}</b>: no swing ${kind} within 8 bars — ${lb} must sit on a clear turning point</li>`;
      if (r.exact) return `<li>✓ <b>${lb}</b> exactly on the swing ${kind} (${fmtPrice(r.real.price)})</li>`;
      const when = r.barsOff === 0 ? "right bar" : `${Math.abs(r.barsOff)} bar${Math.abs(r.barsOff) > 1 ? "s" : ""} ${r.barsOff > 0 ? "late" : "early"}`;
      const where = Math.abs(r.ptsOff) < 0.05 ? "" : `, ${fmtPrice(Math.abs(r.ptsOff), 1)} pts ${r.ptsOff > 0 ? "too high" : "too low"}`;
      const close = Math.abs(r.barsOff) <= 1 && Math.abs(r.ptsOff) <= 3;
      return `<li class="${close ? "" : "down"}">${close ? "≈" : "✗"} <b>${lb}</b>: ${when}${where} — real swing ${kind} ${fmtPrice(r.real.price)} at ${fmtTime(r.real.time)}</li>`;
    }).join("");
    const ratioWhy = (pr) => {
      const rows = classify(pr, { abcd });
      if (!rows.length) return "";
      if (!rows[0].zig) return "the points do not zig-zag (each point must be a high after a low or a low after a high)";
      const r0 = rows[0].checks, val = (leg) => r0.find((c) => c.leg === leg)?.value;
      return abcd ? `BC/AB ${val("C")}` : `B = ${val("B")} of XA, C = ${val("C")} of AB${val("D") != null ? `, D = ${rows.find((x) => x.pid === 0).checks.find((c) => c.leg === "D")?.value} of XA` : ""}`;
    };
    const body = this.card(target ? "✍️ Mark the pattern — graded" : "✔ Check my drawing", `
      <div class="co-board"><b>${exact}/${labels.length}</b> points exactly on the real swing${target ? " of the pattern" : ""}</div>
      <ul class="co-checks">${rows}</ul>
      <p class="small"><b>Your points</b> (${esc(ratioWhy(prices))}) fit: ${yours.length ? `<b>${yours.map(esc).join(", ")}</b>` : `<span class="down">no pattern</span>`}</p>
      <p class="small"><b>${target ? "The real pattern" : "With the real swing points"}</b> (${esc(ratioWhy(realPrices))}): ${real.length ? `<b class="up">${real.map(esc).join(", ")}</b>` : `<span class="down">no harmonic pattern — not every zig-zag is one</span>`}</p>
      <label class="small">What did you think it was? <select class="lb-name"><option value="">—</option>${H_NAMES.map((n) => `<option>${esc(n)}</option>`).join("")}</select></label>
      <div class="lb-verdict small"></div>
      <p class="small dim">Orange dots = the real swing points${target ? " of the pattern" : ""}. Drag your handles onto them and press ✔ again to re-check.</p>
      ${this.drill ? `<button class="lb-next primary">Next ▶</button>` : ""}`);
    const logIt = (answer) => this.log({ mode: target ? "mark" : "check", pattern: real[0] || yours[0] || null, answer: answer || undefined,
      ok: exact >= labels.length - 1 && (!answer || real.includes(answer)) && (!target || res.every((r) => r.real && Math.abs(r.barsOff) <= 1)),
      points: res.map((r, i) => ({ label: labels[i], barsOff: r.barsOff, ptsOff: r.ptsOff == null ? null : Math.round(r.ptsOff * 100) / 100, exact: r.exact })),
      score: exact });
    let logged = false;
    body.querySelector(".lb-name").onchange = (e) => {
      const n = e.target.value, v = body.querySelector(".lb-verdict");
      if (!n) { v.innerHTML = ""; return; }
      const ok = real.includes(n);
      v.innerHTML = ok ? `<span class="up">✓ Right — the swings make a ${esc(n)}.</span>`
        : real.length ? `<span class="down">✗ It is ${real.map(esc).join(" / ")}, not ${esc(n)}.</span> ${esc(why(n, realPrices, abcd))}` : `<span class="down">✗ The real swings make no harmonic pattern.</span>`;
      if (target) { if (!logged) { logged = true; logIt(n); this.drillDone(ok && exact >= labels.length - 1); } }
      else if (!t._named) { t._named = true; this.log({ mode: "name", pattern: real[0] || null, answer: n, ok }); }
    };
    body.querySelector(".lb-next")?.addEventListener("click", () => {
      if (!logged) { logged = true; logIt(""); if (target) this.drillDone(exact >= labels.length - 1); }
      this.nextDrillTask();
    });
    if (!target) { logIt(""); logged = true; }
    panel.view.requestDraw();
  }

  // ═════════════════════════ 2 · Trade-plan drill ═════════════════════════
  async startPlan() {
    const panel = this.app.active, st = panel.state;
    if (!st.first || !st.last) return;
    await this.exitTask(!!this.drill);
    this.card("📝 Trade-plan drill", `<div class="dim">Looking for a setup in history…</div>`);
    let round = null;
    const span = st.last - st.first - 120 * 86400;
    for (let tries = 0; tries < 6 && !round; tries++) {
      const after = Math.floor(st.first + 60 * 86400 + Math.random() * Math.max(span, 86400));
      try { round = await get(`/api/harmonics/challenge?${q({ symbol: st.symbol, tf: st.tf, after })}`); } catch { /* another date */ }
    }
    if (!round) { this.card("📝 Trade-plan drill", `<div class="down">No setup found — try another symbol or timeframe.</div>`); return; }
    const sec = TF_SECONDS[st.tf];
    this.task = { kind: "plan", panel, round, side: round.dir === "bullish" ? 1 : -1, clicks: [], phase: "plan", replay: true, hint: false };
    await this.app.replay.start(panel, null, false, round.start_time + sec);
    panel.view.autoScale();
    this.zoomTo(panel, round.points[0].time, 35);
    this.renderPlan();
  }

  renderPlan() {
    const t = this.task, r = t.round, [entry, stop, target] = t.clicks;
    const side = t.side > 0 ? "▲ LONG (bullish: D will be a low)" : "▼ SHORT (bearish: D will be a high)";
    const names = ["entry", "stop-loss", "target"];
    const risk = entry != null && stop != null ? Math.abs(entry - stop) : null, rew = entry != null && target != null ? Math.abs(target - entry) : null;
    const err = t.clicks.length === 3 ? planError(t.side, entry, stop, target) : null;
    if (t.phase === "plan") {
      this.card("📝 Trade-plan drill", `
        <p><b>${side}</b> — X‑A‑B‑C just formed. Plan the trade <b>before</b> D: a limit order at your entry.</p>
        <ol class="small"><li>Click the chart for the <b>entry</b>${entry != null ? ` → <b>${fmtPrice(entry)}</b>` : ""}</li>
          <li>Click for the <b>stop-loss</b>${stop != null ? ` → <b class="down">${fmtPrice(stop)}</b>` : ""}</li>
          <li>Click for the <b>target</b>${target != null ? ` → <b class="up">${fmtPrice(target)}</b>` : ""}</li></ol>
        ${risk != null ? `<div class="co-board small">Risk ${fmtPrice(risk, 1)} pts (₹${Math.round(risk * LOT).toLocaleString("en-IN")}/lot)${rew != null ? ` · reward ${fmtPrice(rew, 1)} pts · R:R 1:${(rew / (risk || 1)).toFixed(2)}` : ""}</div>` : ""}
        ${err ? `<div class="down small">${esc(err)}</div>` : ""}
        <p class="small">Next click sets the <b>${names[Math.min(t.clicks.length, 2)]}</b>. <button class="lb-reset">↺ Reset</button>
          <button class="lb-hint ${t.hint ? "on" : ""}" title="Shade the D zones of the patterns X-A-B-C fits">💡 D zones</button></p>
        <button class="lb-go primary" ${t.clicks.length === 3 && !err ? "" : "disabled"}>▶ Lock in & play</button>
        <p class="small dim">Rules: the order fills only when price trades at your entry, never from 15:15; an open trade is closed at 15:15 (intraday). If price breaks beyond C first, the setup is dead and the order is cancelled.</p>`);
      const b = this.el.querySelector(".lb-body");
      b.querySelector(".lb-reset").onclick = () => { t.clicks = []; this.renderPlan(); t.panel.view.requestDraw(); };
      b.querySelector(".lb-hint").onclick = () => { t.hint = !t.hint; this.renderPlan(); t.panel.view.requestDraw(); };
      b.querySelector(".lb-go").onclick = () => this.playPlan();
    } else if (t.phase === "play") {
      const s = t.res?.status === "open" ? `filled at ${fmtPrice(t.res.fill.price)} — in the trade` : "waiting for price to reach your entry";
      this.card("📝 Trade-plan drill", `<p>Locked: entry <b>${fmtPrice(entry)}</b>, stop <b class="down">${fmtPrice(stop)}</b>, target <b class="up">${fmtPrice(target)}</b>.</p>
        <p class="small">Playing… ${s}.</p><button class="lb-stop">⏹ Stop and score now</button>`);
      this.el.querySelector(".lb-stop").onclick = () => this.finishPlan(t.res || { status: "waiting" }, true);
    }
  }

  playPlan() {
    const t = this.task, [entry, stop, target] = t.clicks, v = t.panel.view;
    t.plan = { side: t.side, entry, stop, target, from: v.bars[v.bars.length - 1].time, cancel: t.round.points[3].price, maxBars: 150 };
    t.phase = "play";
    this.renderPlan();
    this.app.replay.speed = 20;
    this.app.replay.play();
  }

  finishPlan(res, early = false) {
    const t = this.task, r = t.round, a = r.actual;
    this.app.replay.pause();
    t.phase = "done"; t.res = res;
    const [entry, stop, target] = t.clicks;
    const R = res.status === "done" ? res.R : 0;
    const head = res.status === "done" ? (res.R > 0 ? `🎉 +${res.R.toFixed(2)}R — ${esc(res.reason)}` : `${res.R.toFixed(2)}R — ${esc(res.reason)}`)
      : res.status === "cancelled" ? "⛔ Order cancelled — the setup failed before your entry" : res.status === "expired" ? "⌛ No fill — price never reached your entry" : early ? "⏹ Stopped early" : "…";
    const ad = a ? Math.abs(entry - a.price) : null;
    const tips = [];
    if (a && res.status !== "cancelled") {
      if (t.side > 0 ? stop > a.price : stop < a.price) tips.push(`The real D went to ${fmtPrice(a.price)} — your stop was ${fmtPrice(Math.abs(stop - a.price), 1)} pts inside D's extreme. Stops just beyond X or the far end of the D zone survive more often.`);
      if (res.status === "expired" && (t.side > 0 ? entry < a.price : entry > a.price)) tips.push(`D turned at ${fmtPrice(a.price)}, before your entry — a little less greed on the entry would have filled.`);
    }
    if (res.status === "done" && res.reason === "square-off 15:15") tips.push("Closed at 15:15: intraday plans need targets reachable the same day.");
    const body = this.card("📝 Trade-plan drill — result", `
      <p class="co-win">${head}</p>
      <table class="small"><tr><td>Plan</td><td>entry ${fmtPrice(entry)} · stop ${fmtPrice(stop)} · target ${fmtPrice(target)}</td></tr>
        ${res.fill ? `<tr><td>Fill</td><td>${fmtPrice(res.fill.price)} at ${fmtTime(res.fill.time)}</td></tr>` : ""}
        ${res.exit ? `<tr><td>Exit</td><td>${fmtPrice(res.exit.price)} at ${fmtTime(res.exit.time)} (${fmtPrice(res.pts, 1)} pts = ₹${Math.round(res.pts * LOT).toLocaleString("en-IN")}/lot)</td></tr>` : ""}
        ${a ? `<tr><td>Real D</td><td><b style="color:${REAL}">${fmtPrice(a.price)}</b> at ${fmtTime(a.time)}${ad != null ? ` — your entry was ${fmtPrice(ad, 1)} pts away` : ""}</td></tr>` : ""}
        <tr><td>ChartLab zone</td><td>${esc(r.app.pattern)} ${fmtPrice(r.app.prz[0])}–${fmtPrice(r.app.prz[1])}</td></tr></table>
      ${tips.length ? `<ul class="small">${tips.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
      <button class="lb-again primary">${this.drill ? "Next ▶" : "Next setup"}</button> <button class="lb-quit">Stop</button>`);
    this.log({ mode: "plan", pattern: r.app.pattern, status: res.status, R: Math.round(R * 1000) / 1000, pts: res.pts ?? null, ok: R > 0 });
    if (this.drill) this.drillDone(R > 0);
    body.querySelector(".lb-again").onclick = () => (this.drill ? this.nextDrillTask() : this.startPlan());
    body.querySelector(".lb-quit").onclick = () => this.exitTask();
    t.panel.view.requestDraw();
  }

  onChartClick(panel, p) {
    const t = this.task;
    if (!t || t.kind !== "plan" || t.phase !== "plan" || panel !== t.panel || !p?.point || this.app.replay.picking) return;
    if (panel.drawings.tool !== "cursor") return;
    const price = panel.view.yToPrice(p.point.y);
    if (price == null) return;
    if (t.clicks.length >= 3) t.clicks = [];
    t.clicks.push(Math.round(price * 20) / 20);
    this.renderPlan();
    panel.view.requestDraw();
  }

  // replay step
  onClock() {
    const t = this.task;
    if (!t) return;
    if (t.kind === "plan" && t.phase === "play") {
      const res = simulatePlan(t.panel.view.bars, t.plan);
      const prev = t.res?.status;
      t.res = res;
      if (["done", "cancelled", "expired"].includes(res.status)) this.finishPlan(res);
      else if (res.status !== prev) this.renderPlan();
    }
    if (t.kind === "library") this.renderLibraryItem();
  }

  // ═════════════════════════ 4 · D-zone preview (drawn from drawings.drawPattern) ═════════════════════════
  drawZones(ctx, panel, it, pts) {
    if (!this.przOn || !HARM_TOOLS.has(it.type)) return;
    const abcd = it.type === "abcd", nC = abcd ? 3 : 4;
    if (it.points.length < nC) return;
    const prices = it.points.slice(0, nC).map((p) => p.price);
    const zones = dZones(prices, { abcd, only: it.type === "cypher" ? [CYPHER] : null });
    this.paintZones(ctx, panel.view, pts[nC - 1], pts[nC] || null, zones, prices);
  }

  paintZones(ctx, v, c, d, zones, prices) {
    const w = v.paneRect().w;
    const span = Math.max(30 * (v.chart.timeScale().options().barSpacing || 6), d ? d.x - c.x + 40 : 0);
    const x2 = Math.min(w, c.x + span);
    ctx.save();
    if (!zones.length) {
      label(ctx, "X‑A‑B‑C fits no pattern yet (B or C ratio off)", c.x + 6, c.y + (prices[prices.length - 1] > prices[prices.length - 2] ? -14 : 14), "#787b86", "left", 10);
      ctx.restore(); return;
    }
    const used = [];
    for (const z of zones) {
      const y1 = v.priceToY(z.hi), y2 = v.priceToY(z.lo);
      if (y1 == null || y2 == null) continue;
      const h = Math.max(2, y2 - y1);
      ctx.globalAlpha = 0.16; ctx.fillStyle = z.color; ctx.fillRect(c.x, y1, x2 - c.x, h);
      ctx.globalAlpha = 0.8; ctx.strokeStyle = z.color; ctx.lineWidth = 1; ctx.setLineDash([4, 3]); ctx.strokeRect(c.x, y1, x2 - c.x, h); ctx.setLineDash([]);
      let ly = (y1 + y2) / 2;
      while (used.some((u) => Math.abs(u - ly) < 12)) ly += 12;
      used.push(ly);
      ctx.globalAlpha = 1;
      label(ctx, `${z.name} D ${fmtPrice(z.lo)}–${fmtPrice(z.hi)}`, x2 + 4 > w - 150 ? c.x + 4 : x2 + 4, ly, z.color, "left", 10);
    }
    ctx.restore();
  }

  // ═════════════════════════ 5 · Ratio cheat sheet ═════════════════════════
  updateSheet(panel) {
    if (panel !== this.app.active) return;
    const d = panel.drawings, it = d.draft || d.selected;
    const show = this.sheetOn && it && HARM_TOOLS.has(it.type) && !(this.task?.kind === "quiz" && !this.task.answered);
    if (!show) { if (!this.sheet.hidden) this.sheet.hidden = true; this._sheetKey = null; return; }
    const abcd = it.type === "abcd";
    const prices = it.points.map((p) => p.price);
    const k = it.type + prices.map((p) => p.toFixed(2)).join(",");
    if (k === this._sheetKey) return;
    this._sheetKey = k;
    this.sheet.hidden = false;
    this.sheet.innerHTML = sheetHtml(prices, abcd);
  }

  // ═════════════════════════ 6 · Name that pattern ═════════════════════════
  async startQuiz() {
    const panel = this.app.active;
    await this.exitTask(!!this.drill);
    this.card("🃏 Name that pattern", `<div class="dim">Picking a real pattern from history…</div>`);
    const item = await this.randomItem(panel, (g) => g.points.length === 5 || Math.random() < 0.15);
    if (!item) { this.card("🃏 Name that pattern", `<div class="down">No patterns found — try another symbol or timeframe.</div>`); return; }
    await this.showWindow(panel, item, { before: 40, after: 8 });
    this.task = { kind: "quiz", panel, item, window: true, t0: performance.now(), answered: null };
    this.renderQuiz();
    clearInterval(this._timer);
    this._timer = setInterval(() => {
      const t = this.task;
      if (!t || t.kind !== "quiz" || t.answered) { clearInterval(this._timer); return; }
      const left = 30 - (performance.now() - t.t0) / 1000;
      const el = this.el.querySelector(".lb-timer");
      if (el) el.textContent = left > 0 ? `${Math.ceil(left)} s` : "time!";
      if (left <= 0) this.answerQuiz(null);
    }, 250);
    panel.view.requestDraw();
  }

  renderQuiz() {
    const t = this.task, it = t.item, s = this.streak;
    const opts = (t.item.points.length === 4 ? ["AB=CD", "Gartley", "Bat", "Crab"] : H_NAMES.filter((n) => n !== "AB=CD"));
    if (!t.answered) {
      const body = this.card("🃏 Name that pattern", `
        <div class="co-board">Streak <b>${s.now}</b> · best ${s.best} · <span class="lb-timer">30 s</span></div>
        <p>${it.dir === "bullish" ? "▲ Bullish" : "▼ Bearish"} pattern, real NIFTY data. The leg ratios are on the chart. <b>Which pattern is it?</b></p>
        <div class="lb-opts">${opts.map((n) => `<button data-n="${esc(n)}">${esc(n)}</button>`).join("")}</div>
        <p class="small dim">Tip: look at B first (how deep AB retraced XA), then where D sits against XA.</p>`);
      body.querySelectorAll(".lb-opts button").forEach((b) => (b.onclick = () => this.answerQuiz(b.dataset.n)));
      return;
    }
    const a = t.answered, ok = a.ok;
    const main = it.patterns[0], rows = (it.checks[main] || []).map((c) => `<li class="${c.info ? "dim" : ""}">${c.info ? "·" : "✓"} ${esc(c.text)} <span class="dim">(${esc(main)} needs ${c.need})</span></li>`).join("");
    const out = it.outcome === "worked" ? `<span class="up">worked ✓ (came back ${((it.reaction?.retrace || 0) * 100).toFixed(0)}% of AD)</span>` : it.outcome === "failed" ? `<span class="down">failed ✗${it.broke_x ? " — price went beyond X" : ""}</span>` : "still open";
    const body = this.card("🃏 Name that pattern", `
      <div class="co-board">Streak <b>${s.now}</b> · best ${s.best}</div>
      <p class="co-win">${ok ? "✓ Correct" : a.answer ? `✗ You said ${esc(a.answer)}` : "⌛ Time's up"} — it is <b>${it.patterns.map(esc).join(" / ")}</b></p>
      <ul class="co-checks small">${rows}</ul>
      ${!ok && a.answer ? `<p class="small">${esc(why(a.answer, it.points.map((p) => p.price), it.points.length === 4))}</p>` : ""}
      <p class="small">What happened after D: ${out}. ${fmtTime(it.points[it.points.length - 1].time)}</p>
      <button class="lb-again primary">${this.drill ? "Next ▶" : "Next card"}</button> <button class="lb-quit">Stop</button>`);
    body.querySelector(".lb-again").onclick = () => (this.drill ? this.nextDrillTask() : this.startQuiz());
    body.querySelector(".lb-quit").onclick = () => this.exitTask();
  }

  answerQuiz(name) {
    const t = this.task;
    if (!t || t.answered) return;
    clearInterval(this._timer);
    const ok = !!name && t.item.patterns.includes(name);
    const secs = Math.round((performance.now() - t.t0) / 100) / 10;
    t.answered = { answer: name, ok, secs };
    const s = this.streak;
    s.now = ok ? s.now + 1 : 0; s.best = Math.max(s.best, s.now); store.set("quizStreak", s);
    this.log({ mode: "quiz", pattern: t.item.patterns[0], answer: name || "(time)", ok, secs });
    if (this.drill) this.drillDone(ok);
    this.renderQuiz();
    t.panel.view.requestDraw();
  }

  // ═════════════════════════ 7 · Pattern library ═════════════════════════
  openLibrary() {
    const dlg = $("#dlg-library"), panel = this.app.active, { symbol, tf } = panel.state;
    const from = dlg.querySelector(".lib-from"), to = dlg.querySelector(".lib-to"), pat = dlg.querySelector(".lib-pat");
    if (!pat.options.length) pat.innerHTML = `<option value="">All patterns</option>` + H_NAMES.map((n) => `<option>${esc(n)}</option>`).join("");
    if (!to.value) to.value = new Date((panel.state.last || Date.now() / 1000) * 1000).toISOString().slice(0, 10);
    if (!from.value) from.value = new Date(Date.parse(to.value) - 180 * 86400e3).toISOString().slice(0, 10);
    dlg.querySelector("h3").textContent = `📚 Pattern library · ${symbol} ${tf === "D" ? "daily" : tf + "m"}`;
    const run = async () => {
      const out = dlg.querySelector(".lib-out");
      out.innerHTML = `<div class="dim">Scanning…</div>`;
      try {
        const r = await get(`/api/harmonics/library?${q({ symbol, tf, start: from.value, end: to.value, pattern: pat.value,
          outcome: dlg.querySelector(".lib-out-sel").value, dir: dlg.querySelector(".lib-dir").value, limit: 400 })}`);
        const c = r.counts;
        this._lib = r.items;
        out.innerHTML = `<p class="small">${c.total} patterns (${c.worked} worked, ${c.failed} failed) — showing ${r.items.length} of ${r.matched} matching.
            By type: ${Object.entries(c.by_pattern).map(([k, v]) => `${esc(k)} ${v}`).join(" · ")}</p>
          <div class="table-wrap"><table><tr><th>D completed</th><th>Pattern</th><th>Dir</th><th>Score</th><th>Outcome</th><th>Came back</th><th></th></tr>${r.items.map((g, i) =>
            `<tr class="lib-row" data-i="${i}"><td>${fmtTime(g.points[g.points.length - 1].time)}</td><td>${g.patterns.map(esc).join(" / ")}</td>
             <td class="${g.dir === "bullish" ? "up" : "down"}">${g.dir === "bullish" ? "▲" : "▼"}</td><td>${g.score}</td>
             <td class="${g.outcome === "worked" ? "up" : g.outcome === "failed" ? "down" : "dim"}">${g.outcome}${g.broke_x ? " · broke X" : ""}</td>
             <td>${g.reaction ? (g.reaction.retrace * 100).toFixed(0) + "% of AD" : "—"}</td>
             <td><button class="lib-replay" data-i="${i}" title="Replay from C: watch D form">⏪ at C</button> <button class="lib-show" data-i="${i}" title="Show the whole pattern">👁</button></td></tr>`).join("")}</table></div>
          <p class="small dim">Worked = price came back ≥ 38.2% of AD within 30 bars. Failed ones teach the most: look at what was different.</p>`;
        out.querySelectorAll(".lib-replay").forEach((b) => (b.onclick = () => { dlg.close(); this.openItem(this._lib[+b.dataset.i], true); }));
        out.querySelectorAll(".lib-show").forEach((b) => (b.onclick = () => { dlg.close(); this.openItem(this._lib[+b.dataset.i], false); }));
      } catch (e) { out.innerHTML = `<div class="down">${esc(e.message)}</div>`; }
    };
    dlg.querySelector(".lib-run").onclick = run;
    dlg.showModal();
    run();
  }

  async openItem(item, replay) {
    const panel = this.app.active, sec = TF_SECONDS[panel.state.tf];
    await this.exitTask();
    if (replay) {
      const c = item.points[item.points.length - 2];
      this.task = { kind: "library", panel, item, replay: true };
      await this.app.replay.start(panel, null, false, c.time + 4 * sec);
      panel.view.autoScale();
      this.zoomTo(panel, item.points[0].time, 40);
    } else {
      await this.showWindow(panel, item, { before: 40, after: 30, hideIndicators: false });
      this.task = { kind: "library", panel, item, window: true };
    }
    this.renderLibraryItem();
    panel.view.requestDraw();
  }

  /** how many points are confirmed by now (replay) — a swing is known 3 bars after it */
  knownPoints(t) {
    const K = this.app.replay.active ? this.app.replay.knownUntil : null;
    if (K == null) return t.item.points.length;
    const sec = TF_SECONDS[t.panel.state.tf];
    return t.item.points.filter((p) => p.time + 4 * sec <= K).length;
  }

  renderLibraryItem() {
    const t = this.task, it = t.item, n = this.knownPoints(t), all = n >= it.points.length;
    const key = `${n}`;
    if (t._k === key) return;
    t._k = key;
    const main = it.patterns[0];
    const rows = all ? (it.checks[main] || []).map((c) => `<li class="${c.info ? "dim" : ""}">${c.info ? "·" : "✓"} ${esc(c.text)} <span class="dim">(${esc(main)} needs ${c.need})</span></li>`).join("") : "";
    const out = it.outcome === "worked" ? `<span class="up">worked ✓ — price came back ${((it.reaction?.retrace || 0) * 100).toFixed(0)}% of AD</span>`
      : it.outcome === "failed" ? `<span class="down">failed ✗${it.broke_x ? " — price went beyond X (pattern invalid)" : " — no 38.2% bounce within 30 bars"}</span>` : "not enough bars after D yet";
    const body = this.card("📚 Library example", all
      ? `<div class="co-head ${it.dir === "bullish" ? "up" : "down"}">${it.dir === "bullish" ? "▲" : "▼"} ${it.patterns.map(esc).join(" / ")} · score ${it.score}</div>
         <ul class="co-checks small">${rows}</ul><p class="small">${out}</p>
         <p class="small dim">${t.replay ? "Keep stepping (→) to see the reaction after D." : ""}</p><button class="lb-quit">Close</button>`
      : `<p>${it.dir === "bullish" ? "▲ Bullish" : "▼ Bearish"} setup — ${n} of ${it.points.length} points formed. Where will D land? Press ▶ (or →) to watch.</p>
         <p class="small dim">The name and the outcome appear once D is confirmed (3 bars after it).</p><button class="lb-quit">Close</button>`);
    body.querySelector(".lb-quit").onclick = () => this.exitTask();
    t.panel.view.requestDraw();
  }

  // ═════════════════════════ 8 · Daily drill ═════════════════════════
  async startDrill() {
    const date = today();
    let d = store.get("drill", null);
    if (!d || d.date !== date) {
      const rnd = seeded([...date].reduce((a, ch) => a * 31 + ch.charCodeAt(0), 7) % 2147483647);
      const tasks = ["quiz", "quiz", "quiz", "quiz", "mark", "mark", "mark", "plan", "plan", "plan"];
      for (let i = tasks.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [tasks[i], tasks[j]] = [tasks[j], tasks[i]]; }
      d = { date, tasks, idx: 0, results: [] };
    }
    this.drill = d;
    store.set("drill", d);
    if (d.idx >= d.tasks.length) return this.drillSummary();
    this.runDrillTask();
  }

  runDrillTask() {
    const d = this.drill, k = d.tasks[d.idx];
    if (k === "quiz") return this.startQuiz();
    if (k === "plan") return this.startPlan();
    return this.startMark();
  }

  drillDone(ok) {
    const d = this.drill;
    if (!d || d.results.length > d.idx) return;
    d.results.push({ kind: d.tasks[d.idx], ok });
    store.set("drill", d);
  }

  async nextDrillTask() {
    const d = this.drill;
    if (!d) return this.exitTask();
    if (d.results.length <= d.idx) d.results.push({ kind: d.tasks[d.idx], ok: false });   // skipped
    d.idx++;
    store.set("drill", d);
    await this.exitTask(true);
    if (d.idx >= d.tasks.length) return this.drillSummary();
    this.runDrillTask();
  }

  async drillSummary() {
    const d = this.drill || store.get("drill", null);
    this.drill = null;
    const ok = d.results.filter((r) => r.ok).length;
    if (!d.logged) { d.logged = true; store.set("drill", d); await this.log({ mode: "drill", ok: true, score: ok, drill: d.date }); }
    let log = [];
    try { log = await get("/api/practice"); } catch { /* offline */ }
    const streak = drillStreak(log.filter((e) => e.mode === "drill").map((e) => e.drill || (e.at || "").slice(0, 10)), today());
    const by = (k) => { const r = d.results.filter((x) => x.kind === k); return `${r.filter((x) => x.ok).length}/${r.length}`; };
    const body = this.card("📅 Daily drill — done", `<p class="co-win">${ok}/10 today · 🔥 ${streak}-day streak</p>
      <table class="small"><tr><td>🃏 Name</td><td>${by("quiz")}</td></tr><tr><td>✍️ Mark</td><td>${by("mark")}</td></tr><tr><td>📝 Plan</td><td>${by("plan")}</td></tr></table>
      <p class="small dim">Come back tomorrow for a new set. 📈 My practice stats shows your habits.</p><button class="lb-stats primary">📈 Stats</button> <button class="lb-quit">Close</button>`);
    body.querySelector(".lb-stats").onclick = () => this.openStats();
    body.querySelector(".lb-quit").onclick = () => this.exitTask();
  }

  async startMark() {
    const panel = this.app.active;
    await this.exitTask(true);
    this.card("✍️ Mark the pattern", `<div class="dim">Picking a real pattern…</div>`);
    const item = await this.randomItem(panel, (g) => g.points.length === 5);
    if (!item) { this.card("✍️ Mark the pattern", `<div class="down">No pattern found — try another timeframe.</div><button class="lb-skip">Skip ▶</button>`); this.el.querySelector(".lb-skip").onclick = () => this.nextDrillTask(); return; }
    const bars = await this.showWindow(panel, item, { before: 40, after: 6 });
    this.task = { kind: "mark", panel, item, window: true, drawIds: new Set(), startIds: new Set(panel.drawings.items.map((x) => x.id)) };
    const body = this.card("✍️ Mark the pattern", `
      <p>A ${item.dir === "bullish" ? "▲ bullish" : "▼ bearish"} harmonic pattern completes <b>near the right edge</b> (D is one of the last ~10 candles).</p>
      <ol class="small"><li>⋀⋁ menu → <b>XABCD pattern</b>, click X, A, B, C, D on the swing points.</li><li>Press <b>✔ Check</b> below.</li></ol>
      <button class="lb-check primary">✔ Check</button> <button class="lb-skip">Skip ▶</button>`);
    body.querySelector(".lb-skip").onclick = () => this.nextDrillTask();
    body.querySelector(".lb-check").onclick = () => {
      const t = this.task;
      const mine = panel.drawings.items.filter((x) => x.type === "xabcd" && !t.startIds.has(x.id));
      mine.forEach((x) => t.drawIds.add(x.id));
      const it = mine[mine.length - 1];
      if (!it || it.points.length < 5) { this.app.toast("Draw the XABCD first (all 5 points)"); return; }
      this.check(panel, it, item);
    };
    panel.view.requestDraw();
    return bars;
  }

  // ═════════════════════════ 3 · Stats ═════════════════════════
  async openStats() {
    const dlg = $("#dlg-hstats");
    const out = dlg.querySelector(".hs-out");
    out.innerHTML = `<div class="dim">Loading…</div>`;
    dlg.showModal();
    let log = [];
    try { log = await get("/api/practice"); } catch (e) { out.innerHTML = `<div class="down">${esc(e.message)}</div>`; return; }
    const S = practiceStats(log), hints = mistakeHints(S);
    const pct = (a, b) => (b ? `${Math.round((100 * a) / b)}%` : "—");
    const MODES = { check: "✔ Check my drawing", name: "🏷 Naming your drawing", mark: "✍️ Mark (drill)", quiz: "🃏 Name that pattern", plan: "📝 Trade plans", drill: "📅 Daily drills done" };
    const ch = store.get("challengeScore", null);
    const days = Object.entries(S.days).sort().slice(-14);
    const streak = drillStreak(log.filter((e) => e.mode === "drill").map((e) => e.drill || (e.at || "").slice(0, 10)), today());
    out.innerHTML = log.length ? `
      <div class="hs-grid">
        <div><h4>By exercise</h4><table>${Object.entries(MODES).filter(([k]) => S.modes[k]).map(([k, n]) => `<tr><td>${n}</td><td><b>${S.modes[k].n}</b></td><td>${k === "drill" ? `🔥 ${streak}-day streak` : `${pct(S.modes[k].ok, S.modes[k].n)} right`}</td></tr>`).join("")}
          ${ch && ch.rounds ? `<tr><td>🏆 Challenge (coach)</td><td><b>${ch.rounds}</b></td><td>you won ${ch.you}, ChartLab ${ch.app}</td></tr>` : ""}</table>
          ${S.plan.n ? `<h4>Trade plans</h4><p>${S.plan.n} plans · ${S.plan.filled} filled · ${pct(S.plan.wins, S.plan.filled)} winners · total <b class="${S.plan.R >= 0 ? "up" : "down"}">${S.plan.R.toFixed(2)}R</b>${S.plan.filled ? ` (avg ${(S.plan.R / S.plan.filled).toFixed(2)}R per filled plan)` : ""}</p>` : ""}
          <h4>By pattern</h4><table><tr><th>Pattern</th><th>Tries</th><th>Right</th></tr>${Object.entries(S.patterns).sort((a, b) => b[1].n - a[1].n).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${v.n}</td><td>${pct(v.ok, v.n)}</td></tr>`).join("")}</table></div>
        <div><h4>Your points (check & mark)</h4><table><tr><th></th><th>Exact</th><th>Early</th><th>Late</th><th>Avg off</th><th>No swing</th></tr>${["X", "A", "B", "C", "D"].filter((l) => S.points[l]).map((l) => { const p = S.points[l]; return `<tr><td><b>${l}</b></td><td>${pct(p.exact, p.n)}</td><td>${pct(p.early, p.n)}</td><td>${pct(p.late, p.n)}</td><td>${p.n ? (p.absBars / p.n).toFixed(1) + " bars" : "—"}</td><td>${pct(p.missed, p.n + p.missed)}</td></tr>`; }).join("")}</table>
          <h4>Habits to fix</h4>${hints.length ? `<ul>${hints.map((h) => `<li>${esc(h)}</li>`).join("")}</ul>` : `<p class="dim">Not enough attempts yet — keep practising.</p>`}
          <h4>Last 14 days</h4><div class="hs-days">${days.map(([d, v]) => `<div title="${d}: ${v.ok}/${v.n} right"><span style="height:${Math.max(4, Math.round((40 * v.ok) / Math.max(1, v.n)))}px"></span><i>${d.slice(8)}</i></div>`).join("")}</div></div>
      </div>
      <p class="small dim">${S.total} attempts saved in data/harmonic_practice.json. <button class="hs-clear">Clear history</button></p>`
      : `<p class="dim">No practice yet. Try 🃏 Name that pattern or 📅 Daily drill.</p>`;
    out.querySelector(".hs-clear")?.addEventListener("click", async () => {
      if (!confirm("Delete your whole harmonic practice history?")) return;
      await fetch("/api/practice", { method: "DELETE" });
      this.openStats();
    });
  }

  // ═════════════════════════ drawing ═════════════════════════
  draw(ctx, panel) {
    this.updateSheet(panel);
    const t = this.task;
    if (!t || t.panel !== panel) return;
    const v = panel.view;
    if (t.kind === "plan") this.drawPlan(ctx, v, t);
    if (t.kind === "quiz") this.drawItemPattern(ctx, v, t.item, t.item.points.length, { ratios: true, name: t.answered ? t.item.patterns.join(" / ") : "?" });
    if (t.kind === "library") this.drawItemPattern(ctx, v, t.item, this.knownPoints(t), { ratios: true, name: this.knownPoints(t) >= t.item.points.length ? t.item.patterns.join(" / ") : "" });
    if (t.checked) this.drawCheck(ctx, v, t.checked);
  }

  drawItemPattern(ctx, v, item, n, { ratios = false, name = "" } = {}) {
    const col = item.dir === "bullish" ? BULL : BEAR;
    const abcd = item.points[0].label === "A";
    const P = item.points.slice(0, n), pts = P.map((p) => v.project(p));
    if (pts.some((p) => !p) || !pts.length) return;
    ctx.save();
    ctx.globalAlpha = 0.13; ctx.fillStyle = col;
    const tri = (i, j, k) => { if (k >= pts.length) return; ctx.beginPath(); ctx.moveTo(pts[i].x, pts[i].y); ctx.lineTo(pts[j].x, pts[j].y); ctx.lineTo(pts[k].x, pts[k].y); ctx.closePath(); ctx.fill(); };
    if (!abcd) { tri(0, 1, 2); tri(2, 3, 4); }
    ctx.globalAlpha = 1; ctx.strokeStyle = col; ctx.lineWidth = 2;
    ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y))); ctx.stroke();
    if (ratios && n >= 3) {
      const pr = P.map((p) => p.price), type = abcd ? "abcd" : "xabcd";
      ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
      for (const { i, j, value } of patternRatios(type, pr)) {
        if (j >= n || !isFinite(value)) continue;
        ctx.beginPath(); ctx.moveTo(pts[i].x, pts[i].y); ctx.lineTo(pts[j].x, pts[j].y); ctx.stroke();
        badge(ctx, value.toFixed(3), (pts[i].x + pts[j].x) / 2, (pts[i].y + pts[j].y) / 2, "#363a45");
      }
      ctx.setLineDash([]);
    }
    const labels = abcd ? LABELS4 : LABELS5, pr = item.points.map((p) => p.price);
    pts.forEach((p, i) => badge(ctx, item.points[i].label || labels[i], p.x, p.y + (isHigh(pr, i) ? -14 : 14), col));
    if (name && n >= item.points.length) {
      const d = pts[pts.length - 1];
      badge(ctx, name, d.x + 30, d.y + (isHigh(pr, pr.length - 1) ? -30 : 30), name === "?" ? "#787b86" : col);
    }
    ctx.restore();
  }

  drawPlan(ctx, v, t) {
    const r = t.round, w = v.paneRect().w;
    this.drawItemPattern(ctx, v, { points: r.points, dir: r.dir }, r.points.length, { ratios: true });
    if (t.hint && t.phase === "plan") {
      const c = v.project(r.points[3]);
      if (c) this.paintZones(ctx, v, c, null, dZones(r.points.map((p) => p.price)), r.points.map((p) => p.price));
    }
    const c = v.project(r.points[3]);
    const x0 = c ? c.x : 0;
    const line = (price, color, text) => {
      const y = v.priceToY(price); if (y == null) return;
      ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.setLineDash(text.startsWith("ENTRY") ? [] : [6, 4]);
      ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(w, y); ctx.stroke(); ctx.restore();
      badge(ctx, text, w - 110, y - 10, color);
    };
    const [e, s, tg] = t.clicks;
    if (e != null) line(e, "#d1d4dc", `ENTRY ${fmtPrice(e)}`);
    if (s != null) line(s, BEAR, `STOP ${fmtPrice(s)}`);
    if (tg != null) line(tg, BULL, `TARGET ${fmtPrice(tg)}`);
    const res = t.res;
    if (res?.fill) { const f = v.project(res.fill); if (f) { ctx.fillStyle = "#d1d4dc"; ctx.beginPath(); ctx.arc(f.x, f.y, 5, 0, 7); ctx.fill(); } }
    if (res?.exit) { const f = v.project(res.exit); if (f) badge(ctx, res.R >= 0 ? `+${res.R.toFixed(2)}R` : `${res.R.toFixed(2)}R`, f.x, f.y - 14, res.R >= 0 ? BULL : BEAR); }
    if (t.phase === "done" && r.actual) {
      const d = v.project({ time: r.actual.time, price: r.actual.price });
      if (d) { ctx.fillStyle = REAL; ctx.beginPath(); ctx.arc(d.x, d.y, 5, 0, 7); ctx.fill(); badge(ctx, `real D ${fmtPrice(r.actual.price)}`, d.x, d.y + (t.side > 0 ? 16 : -16), REAL); }
    }
  }

  drawCheck(ctx, v, ch) {
    if (!ch.it || !this.task?.panel.drawings.items.includes(ch.it)) return;
    const real = ch.res.map((r) => (r.real ? v.project(r.real) : null));
    ctx.save();
    ctx.strokeStyle = REAL; ctx.lineWidth = 1.5; ctx.setLineDash([3, 3]);
    ctx.beginPath(); let first = true;
    for (const p of real) { if (!p) { first = true; continue; } first ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y); first = false; }
    ctx.stroke(); ctx.setLineDash([]);
    real.forEach((p, i) => {
      if (!p) return;
      ctx.fillStyle = REAL; ctx.beginPath(); ctx.arc(p.x, p.y, 4.5, 0, 7); ctx.fill();
      if (!ch.res[i].exact) {
        const y = v.project(ch.it.points[i]);
        if (y) { ctx.strokeStyle = YOU; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(y.x, y.y); ctx.lineTo(p.x, p.y); ctx.stroke(); }
      }
    });
    ctx.restore();
  }
}

