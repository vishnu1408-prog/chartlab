// Pattern Coach.
//  🤖 Auto-mark: ChartLab finds harmonic patterns itself (confirmed swings only — in replay only data already revealed)
//     and draws them leg by leg like a person would, with a card explaining every Fibonacci check in plain words.
//  🏆 Challenge: ChartLab finds a moment in history where X-A-B-C has just formed. You click where you think D will
//     complete (and whether it will reverse); ChartLab locks in its own answer (its PRZ). The replay plays until the real
//     D forms — the closer answer wins the round.
import { api, store } from "./api.js";
import { fmtPrice, fmtTime, TF_SECONDS } from "./core.js";
import { esc } from "./indicators.js";
import { badge, label } from "./chart.js";

const get = async (url) => { const r = await fetch(url); const d = await r.json(); if (!r.ok) throw new Error(d.detail || r.statusText); return d; };
const q = (o) => Object.entries(o).filter(([, v]) => v != null).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
const BULL = "#26a69a", BEAR = "#ef5350", APP = "#2962ff", YOU = "#e040fb", REAL = "#f5a623";

export class PatternCoach {
  constructor(app) {
    this.app = app;
    this.on = store.get("coachOn", false);
    this.results = new Map();       // panel → scan result
    this.seen = new Map();          // panel → newest known_at already announced
    this.sel = null;                // selected pattern
    this.anim = null;               // {item, t0}
    this.ch = null;                 // challenge round state
    this.score = store.get("challengeScore", { rounds: 0, you: 0, app: 0, tie: 0, youErr: 0, appErr: 0, revOk: 0 });
    this.el = document.getElementById("coach");
    this.el.querySelector(".co-close").onclick = () => { if (this.ch) this.endChallenge(); else this.toggle(false); };
    this.study = null;          // {date, items, idx, panel} — studying one day
    this.days = null;           // busy-days list
    const bar = this.el.querySelector(".co-study");
    bar.querySelector(".co-date").onchange = (e) => e.target.value && this.studyDate(e.target.value);
    bar.querySelector(".co-days").onclick = () => this.openDays();
    bar.querySelector(".co-prev").onclick = () => this.stepPattern(-1);
    bar.querySelector(".co-next-p").onclick = () => this.stepPattern(1);
  }

  /** 📅 study one date: chart jumps to that day, every pattern completed on it is listed (◀ ▶ to step through) */
  async studyDate(date, idx = 0) {
    const panel = this.app.active, { symbol, tf } = panel.state;
    if (this.ch) this.endChallenge();
    if (this.app.replay.active) this.app.replay.exit(false);
    this.on = true; store.set("coachOn", true); this.app.markCoach?.(); this.el.hidden = false;
    this.el.querySelector(".co-body").innerHTML = `<div class="dim">Loading ${esc(date)}…</div>`;
    let r;
    try { r = await get(`/api/harmonics/day?${q({ symbol, tf, date })}`); }
    catch (e) { this.el.querySelector(".co-body").innerHTML = `<div class="down small">${esc(e.message)}</div>`; return; }
    const c = await api.candles({ symbol, tf, start: r.window[0], end: r.window[1], limit: 20000 });
    panel.closeLive();
    panel.view.knownUntil = null;
    panel.view.setBars(c.bars, tf);
    panel.view.autoScale();
    Object.assign(panel.state, { start: r.window[0], end: r.window[1] });
    panel.indicators.refresh();
    panel.ohlcLegend({});
    this.study = { date, items: r.patterns, idx: Math.min(idx < 0 ? r.patterns.length - 1 : idx, Math.max(0, r.patterns.length - 1)), panel, symbol, tf };
    this.el.querySelector(".co-date").value = date;
    this.results.set(panel, { completed: r.patterns, forming: [] });
    if (r.patterns.length) this.select(r.patterns[this.study.idx]);
    else {
      try { panel.view.chart.timeScale().setVisibleRange({ from: Date.parse(date + "T09:00:00Z") / 1000, to: Date.parse(date + "T15:30:00Z") / 1000 }); } catch { /* ignore */ }
      this.render();
    }
  }

  /** ◀ ▶ previous / next pattern of the studied day (at the ends: previous / next day in the busy-days list) */
  stepPattern(dir) {
    const st = this.study;
    if (!st) { this.app.toast("Pick a date (📅) or a busy day (🔎) first"); return; }
    const j = st.idx + dir;
    if (j >= 0 && j < st.items.length) { st.idx = j; this.select(st.items[j]); return; }
    const list = this.days?.top?.map((d) => d.date);
    if (list?.length) {
      const k = list.indexOf(st.date);
      const nd = list[(k + (dir > 0 ? 1 : -1) + list.length) % list.length];
      return this.studyDate(nd, dir > 0 ? 0 : -1);
    }
    this.app.toast(dir > 0 ? "Last pattern of this day — pick another date or open 🔎 Busy days" : "First pattern of this day");
  }

  endStudy() {
    const p = this.study?.panel;
    this.study = null;
    if (p) p.load();
  }

  /** 🔎 days with the most pattern formations */
  async openDays() {
    const dlg = document.getElementById("dlg-days"), panel = this.app.active, { symbol, tf } = panel.state;
    const from = dlg.querySelector(".dy-from"), to = dlg.querySelector(".dy-to");
    if (!to.value) to.value = new Date((panel.state.last || Date.now() / 1000) * 1000).toISOString().slice(0, 10);
    if (!from.value) from.value = new Date(Date.parse(to.value) - 365 * 86400e3).toISOString().slice(0, 10);
    dlg.querySelector("h3").textContent = `🔎 Busy pattern days · ${symbol} ${tf === "D" ? "daily" : tf + "m"}`;
    const run = async () => {
      const out = dlg.querySelector(".dy-out");
      out.innerHTML = `<div class="dim">Scanning…</div>`;
      try {
        this.days = await get(`/api/harmonics/days?${q({ symbol, tf, start: from.value, end: to.value, top: 60 })}`);
        const d = this.days;
        out.innerHTML = `<p class="small">${d.total_patterns} patterns on ${d.days_with_patterns} days. Busiest first — click a day to study it (◀ ▶ in the coach card moves through the patterns, then to the next day in this list).</p>
          <div class="table-wrap"><table><tr><th>Date</th><th>Patterns</th><th>Types</th><th>▲/▼</th><th>Worked*</th></tr>${d.top.map((g) =>
            `<tr class="dy-row" data-d="${g.date}"><td>${g.date} <span class="dim">${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][new Date(g.date).getUTCDay()]}</span></td><td><b>${g.count}</b></td>
             <td>${Object.entries(g.patterns).map(([k, v]) => `${esc(k)}${v > 1 ? " ×" + v : ""}`).join(", ")}</td><td>${g.bull}/${g.bear}</td><td>${g.worked}/${g.count}</td></tr>`).join("")}</table></div>
          <p class="small dim">* worked = price came back at least 38.2% of AD within 30 bars after D.</p>`;
        out.querySelectorAll(".dy-row").forEach((tr) => (tr.onclick = () => { dlg.close(); this.studyDate(tr.dataset.d); }));
      } catch (e) { out.innerHTML = `<div class="down">${esc(e.message)}</div>`; }
    };
    dlg.querySelector(".dy-run").onclick = run;
    dlg.showModal();
    run();
  }

  attach(panel) {
    panel.view.layers.push((ctx) => this.draw(ctx, panel));
    panel.view.chart.subscribeClick((p) => this.onChartClick(panel, p));
  }

  toggle(on = !this.on) {
    this.on = on;
    store.set("coachOn", on);
    this.el.hidden = !on && !this.ch;
    if (on) this.refresh(); else { this.results.clear(); this.sel = null; this.app.panels.forEach((p) => p.view.requestDraw()); }
    this.app.markCoach?.();
  }

  /** new data (live bar / replay step): rescan, throttled */
  onClock() {
    if (this.ch) this.checkChallenge();
    if (!this.on) return;
    clearTimeout(this._t);
    this._t = setTimeout(() => this.refresh(), 350);
  }

  async refresh(panel = this.app.active) {
    if (!this.on || !panel?.view.bars.length) return;
    if (this.study && (this.study.panel !== panel || this.study.symbol !== panel.state.symbol || this.study.tf !== panel.state.tf)) this.study = null;
    if (this.study && !this.app.replay.active) { this.render(); return; }     // studying a date: keep its list
    const rp = this.app.replay;
    // replay: only candles that have CLOSED by the replay clock (the forming candle would leak its future high/low)
    const K = rp.active ? panel.view.knownUntil : null;
    const end = K != null ? K - TF_SECONDS[panel.state.tf] + 1 : null;
    try {
      const r = await get(`/api/harmonics/scan?${q({ symbol: panel.state.symbol, tf: panel.state.tf, end, bars: 400 })}`);
      this.results.set(panel, r);
      const newest = r.completed[r.completed.length - 1];
      const prev = this.seen.get(panel);
      if (newest && prev != null && newest.known_at > prev) { this.select(newest, false); this.app.toast(`🤖 Found a ${newest.dir} ${newest.pattern} — see how it is marked`); }
      else if (!this.sel || !r.completed.concat(r.forming).some((x) => same(x, this.sel))) this.sel = newest || r.forming[0] || null;
      this.seen.set(panel, newest ? newest.known_at : null);
      this.render();
      panel.view.requestDraw();
    } catch (e) { this.el.querySelector(".co-body").innerHTML = `<div class="down small">${esc(e.message)}</div>`; }
  }

  select(item, scroll = true) {
    this.sel = item;
    this.anim = { item, t0: performance.now() };
    const v = this.app.active.view;
    if (scroll && item) {
      const t = item.points.map((p) => p.time);
      try { v.chart.timeScale().setVisibleRange({ from: Math.min(...t) - 20 * v.tfSec, to: Math.max(...t) + 20 * v.tfSec }); } catch { /* outside data */ }
    }
    const tick = () => { v.requestDraw(); if (this.anim && performance.now() - this.anim.t0 < 2600) requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
    this.render();
  }

  // ── drawing ──
  draw(ctx, panel) {
    const v = panel.view;
    const r = this.results.get(panel);
    if (this.on && r) {
      for (const it of r.completed) if (!same(it, this.sel)) this.drawPattern(ctx, v, it, { faint: true });
      for (const it of r.forming) if (!same(it, this.sel)) this.drawPattern(ctx, v, it, { faint: true });
      if (this.sel && (r.completed.some((x) => same(x, this.sel)) || r.forming.some((x) => same(x, this.sel)))) this.drawPattern(ctx, v, this.sel, {});
    }
    if (this.ch && panel === this.ch.panel) this.drawChallenge(ctx, v);
  }

  drawPattern(ctx, v, it, { faint }) {
    const col = it.dir === "bullish" ? BULL : BEAR;
    const pts = it.points.map((p) => v.project(p));
    if (pts.some((p) => !p)) return;
    let legs = pts.length - 1, frac = 1;
    if (!faint && this.anim && same(this.anim.item, it)) {          // leg-by-leg animation, 450 ms per leg
      const k = (performance.now() - this.anim.t0) / 450;
      legs = Math.min(pts.length - 1, Math.floor(k)); frac = Math.min(1, k - legs);
    }
    ctx.save();
    ctx.globalAlpha = faint ? 0.35 : 1;
    ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = faint ? 1 : 2;
    if (!faint && legs >= pts.length - 1) {                          // fill once complete
      ctx.globalAlpha = 0.13;
      const tris = pts.length === 5 ? [[0, 1, 2], [2, 3, 4]] : pts.length === 4 && it.prz ? [[0, 1, 2], [1, 2, 3]] : [];
      for (const tri of tris) { ctx.beginPath(); tri.forEach((i, k) => (k ? ctx.lineTo(pts[i].x, pts[i].y) : ctx.moveTo(pts[i].x, pts[i].y))); ctx.closePath(); ctx.fill(); }
      ctx.globalAlpha = 1;
    }
    ctx.beginPath(); ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 1; i <= legs && i < pts.length; i++) ctx.lineTo(pts[i].x, pts[i].y);
    if (legs < pts.length - 1) {
      const a = pts[legs], b = pts[legs + 1];
      ctx.lineTo(a.x + (b.x - a.x) * frac, a.y + (b.y - a.y) * frac);
    }
    ctx.stroke();
    const shown = Math.min(pts.length, legs + (frac >= 1 ? 2 : 1));
    if (!faint) {
      for (let i = 0; i < shown; i++) {
        const up = it.points[i].price >= Math.max(it.points[i - 1]?.price ?? -Infinity, it.points[i + 1]?.price ?? -Infinity);
        badge(ctx, it.points[i].label, pts[i].x, pts[i].y + (up ? -13 : 13), col);
      }
    }
    if (it.prz) {                                                   // forming: projected D zone to the right of C
      const y1 = v.priceToY(it.prz[0]), y2 = v.priceToY(it.prz[1]);
      const c = pts[pts.length - 1], x2 = c.x + 40 * Math.max(4, v.chart.timeScale().options().barSpacing || 6);
      if (y1 != null && y2 != null) {
        ctx.globalAlpha = faint ? 0.1 : 0.2; ctx.fillRect(c.x, Math.min(y1, y2), x2 - c.x, Math.abs(y2 - y1));
        ctx.globalAlpha = faint ? 0.35 : 1; ctx.setLineDash([5, 4]); ctx.strokeRect(c.x, Math.min(y1, y2), x2 - c.x, Math.abs(y2 - y1)); ctx.setLineDash([]);
        if (!faint) label(ctx, `D zone (${it.pattern}) ${fmtPrice(it.prz[0])}–${fmtPrice(it.prz[1])}`, c.x + 4, Math.min(y1, y2) - 8, col, "left", 10);
      }
    }
    ctx.restore();
  }

  // ── coach card ──
  render() {
    if (this.ch) return this.renderChallenge();
    const body = this.el.querySelector(".co-body");
    this.el.hidden = !this.on;
    this.el.querySelector(".co-title").textContent = "🤖 Pattern coach — auto-marking";
    const r = this.results.get(this.app.active);
    if (!r) { body.innerHTML = `<div class="dim small">Scanning…</div>`; return; }
    const it = this.sel;
    const st = this.study && !this.app.replay.active ? this.study : null;
    const list = st ? st.items : [...r.forming.map((x) => ({ ...x, forming: true })), ...[...r.completed].reverse()].slice(0, 12);
    const row = (x) => `<button class="co-item${same(x, it) ? " on" : ""}" data-k="${esc(key(x))}"><span class="${x.dir === "bullish" ? "up" : "down"}">${x.dir === "bullish" ? "▲" : "▼"} ${esc(x.pattern)}</span>
      <span class="dim">${x.forming ? "forming" : (this.app.replay.blind ? "" : fmtTime(x.points[x.points.length - 1].time))}</span>${x.forming ? "" : result(x)}</button>`;
    const head = st ? `<div class="co-board">📅 <b>${esc(st.date)}</b> · ${st.items.length} pattern${st.items.length === 1 ? "" : "s"}${st.items.length ? ` · showing ${st.idx + 1}/${st.items.length}` : ""}
        <button class="co-replay" title="Replay this day with the coach marking patterns as they form">⏪ Replay day</button> <button class="co-live" title="Back to the latest data">✕ Back</button></div>` : "";
    body.innerHTML = head + (it ? explain(it) : `<div class="dim small">${st ? "No completed pattern on this day — try 🔎 Busy days." : "No pattern in the last 400 bars with these rules — pick a date (📅), open 🔎 Busy days, or replay."}</div>`)
      + `<div class="co-list">${list.map(row).join("") || ""}</div>`
      + `<div class="dim small">Only confirmed swings (3 bars each side) are used, so a pattern appears a few bars after D — like in real time.</div>`;
    body.querySelectorAll(".co-item").forEach((b) => (b.onclick = () => {
      const x = list.find((y) => key(y) === b.dataset.k);
      if (st) st.idx = st.items.indexOf(x);
      this.select(x);
    }));
    body.querySelector(".co-live")?.addEventListener("click", () => this.endStudy());
    body.querySelector(".co-replay")?.addEventListener("click", () => { const d = st.date; this.study = null; this.app.replay.startAtDate(d); });
  }

  // ── challenge ──
  async startChallenge() {
    const panel = this.app.active, st = panel.state;
    if (!st.first || !st.last) return;
    this.toggleCard();
    this.el.querySelector(".co-body").innerHTML = `<div class="dim">Looking for a pattern in history…</div>`;
    const span = st.last - st.first - 120 * 86400;
    let round = null;
    for (let tries = 0; tries < 6 && !round; tries++) {
      const after = Math.floor(st.first + 60 * 86400 + Math.random() * Math.max(span, 86400));
      try { round = await get(`/api/harmonics/challenge?${q({ symbol: st.symbol, tf: st.tf, after })}`); } catch { /* try another date */ }
    }
    if (!round) { this.app.toast("No pattern found — try another symbol or timeframe", "error"); return; }
    this.ch = { panel, round, phase: "mark", you: null, rev: null };
    const tfSec = TF_SECONDS[st.tf];
    if (this.app.replay.active) this.app.replay.exit(false);
    await this.app.replay.start(panel, null, false, round.start_time + tfSec);
    panel.view.autoScale();
    try {
      const t = round.points.map((p) => p.time);
      panel.view.chart.timeScale().setVisibleRange({ from: Math.min(...t) - 30 * tfSec, to: round.start_time + 40 * tfSec });
    } catch { /* ignore */ }
    this.render();
    panel.view.requestDraw();
  }

  toggleCard() { this.el.hidden = false; }

  onChartClick(panel, p) {
    if (!this.ch || this.ch.phase !== "mark" || panel !== this.ch.panel || !p?.point || this.app.replay.picking) return;
    const price = panel.view.yToPrice(p.point.y);
    if (price == null) return;
    this.ch.you = Math.round(price * 20) / 20;
    this.render(); panel.view.requestDraw();
  }

  answerReverse(yes) { if (this.ch) { this.ch.rev = yes; this.render(); } }

  play() {
    const c = this.ch;
    if (!c || c.you == null) return;
    c.phase = "play";
    this.render();
    this.app.replay.speed = 20;                                    // fast-forward to the result
    this.app.replay.play();
  }

  checkChallenge() {
    const c = this.ch;
    if (!c || c.phase !== "play") return;
    const K = this.app.replay.knownUntil;
    const tfSec = TF_SECONDS[c.panel.state.tf];
    if (K == null || K < c.round.actual.resolved_at + tfSec) return;
    this.app.replay.pause();
    c.phase = "reveal";
    const a = c.round.actual, appG = c.round.app.guess;
    const youErr = Math.abs(c.you - a.price), appErr = Math.abs(appG - a.price);
    const revTrue = !!a.reaction?.t1_hit;
    c.result = { youErr, appErr, winner: Math.abs(youErr - appErr) < 1 ? "tie" : youErr < appErr ? "you" : "app", revTrue };
    const s = this.score;
    s.rounds++; s[c.result.winner]++; s.youErr += youErr; s.appErr += appErr; if (c.rev != null && c.rev === revTrue) s.revOk++;
    store.set("challengeScore", s);
    this.render();
    c.panel.view.requestDraw();
  }

  endChallenge() {
    this.ch = null;
    this.el.hidden = !this.on;
    this.app.panels.forEach((p) => p.view.requestDraw());
    this.render();
  }

  drawChallenge(ctx, v) {
    const c = this.ch, r = c.round;
    this.drawPattern(ctx, v, { ...r, pattern: "", dir: r.dir }, { faint: false });
    const w = v.paneRect().w;
    const hline = (price, color, text, dash) => {
      const y = v.priceToY(price); if (y == null) return;
      ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.setLineDash(dash || []); ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); ctx.restore();
      badge(ctx, text, w - 90, y - 10, color);
    };
    if (c.you != null) hline(c.you, YOU, `YOU ${fmtPrice(c.you)}`, [6, 4]);
    if (c.phase === "reveal") {
      const [lo, hi] = r.app.prz, y1 = v.priceToY(lo), y2 = v.priceToY(hi);
      if (y1 != null && y2 != null) { ctx.save(); ctx.globalAlpha = 0.18; ctx.fillStyle = APP; ctx.fillRect(0, Math.min(y1, y2), w, Math.abs(y2 - y1)); ctx.restore(); }
      hline(r.app.guess, APP, `CHARTLAB ${fmtPrice(r.app.guess)}`, [2, 3]);
      const d = v.project({ time: r.actual.time, price: r.actual.price });
      if (d) { badge(ctx, `real D ${fmtPrice(r.actual.price)}`, d.x, d.y + (r.dir === "bullish" ? 16 : -16), REAL); ctx.save(); ctx.fillStyle = REAL; ctx.beginPath(); ctx.arc(d.x, d.y, 5, 0, 7); ctx.fill(); ctx.restore(); }
    }
  }

  renderChallenge() {
    const c = this.ch, r = c.round, s = this.score, body = this.el.querySelector(".co-body");
    this.el.hidden = false;
    this.el.querySelector(".co-title").textContent = `🏆 Pattern challenge — round ${s.rounds + (c.phase === "reveal" ? 0 : 1)}`;
    const board = `<div class="co-board">You <b>${s.you}</b> · ChartLab <b>${s.app}</b> · ties ${s.tie}${s.rounds ? ` · avg error you ${fmtPrice(s.youErr / s.rounds, 1)} / app ${fmtPrice(s.appErr / s.rounds, 1)} pts` : ""}</div>`;
    const side = r.dir === "bullish" ? "low (bullish: D below C, then up)" : "high (bearish: D above C, then down)";
    if (c.phase === "mark") {
      body.innerHTML = board + `<p><b>${r.dir === "bullish" ? "▲ Bullish" : "▼ Bearish"} setup:</b> X‑A‑B‑C is marked. Where will <b>D</b> complete? D will be a ${side}.</p>
        <ol class="small"><li>Measure XA and AB (Fibonacci tool helps). Common D levels: 0.786 / 0.886 of XA (Gartley / Bat), 1.272–1.618 of XA (Butterfly / Crab).</li>
        <li><b>Click the chart</b> at your D price${c.you != null ? ` — now <b class="co-you">${fmtPrice(c.you)}</b> (click again to change)` : ""}.</li>
        <li>Will price reverse at least 38.2% of AD after D? <button class="co-yes ${c.rev === true ? "on" : ""}">Yes</button> <button class="co-no ${c.rev === false ? "on" : ""}">No</button></li></ol>
        <button class="co-play primary" ${c.you == null ? "disabled" : ""}>▶ Lock in & play to the result</button>
        <div class="dim small">Don't step the replay forward before locking in — that would show the answer.</div>`;
    } else if (c.phase === "play") {
      body.innerHTML = board + `<p>Locked: you <b class="co-you">${fmtPrice(c.you)}</b>. ChartLab has locked its answer too. Playing until D forms…</p>`;
    } else {
      const R = c.result, a = r.actual;
      const win = R.winner === "you" ? "🎉 You win this round" : R.winner === "app" ? "🤖 ChartLab wins this round" : "🤝 Tie";
      body.innerHTML = board + `<p class="co-win">${win}</p>
        <table class="small"><tr><td>Real D</td><td><b style="color:${REAL}">${fmtPrice(a.price)}</b>${a.turned_back ? "" : " (no clear bounce within 150 bars)"}</td></tr>
        <tr><td>You</td><td><b class="co-you">${fmtPrice(c.you)}</b> — off by ${fmtPrice(R.youErr, 1)} pts</td></tr>
        <tr><td>ChartLab</td><td><b style="color:${APP}">${fmtPrice(r.app.guess)}</b> — ${esc(r.app.pattern)} zone ${fmtPrice(r.app.prz[0])}–${fmtPrice(r.app.prz[1])}, off by ${fmtPrice(R.appErr, 1)} pts${a.in_zone ? " ✓ D landed in its zone" : ""}</td></tr>
        <tr><td>Reversal</td><td>${R.revTrue ? "Yes — price came back ≥ 38.2% of AD" : "No — it did not come back 38.2% of AD"}${c.rev == null ? "" : c.rev === R.revTrue ? " · your call ✓" : " · your call ✗"}</td></tr></table>
        <p class="small dim">Other patterns ChartLab considered: ${r.app.candidates.map((x) => `${esc(x.pattern)} ${fmtPrice(x.prz[0])}–${fmtPrice(x.prz[1])}`).join(" · ")}.
        Step back (←) to study how D formed.</p>
        <button class="co-next primary">Next round</button> <button class="co-stop">Stop challenge</button>`;
    }
    body.querySelector(".co-yes")?.addEventListener("click", () => this.answerReverse(true));
    body.querySelector(".co-no")?.addEventListener("click", () => this.answerReverse(false));
    body.querySelector(".co-play")?.addEventListener("click", () => this.play());
    body.querySelector(".co-next")?.addEventListener("click", () => this.startChallenge());
    body.querySelector(".co-stop")?.addEventListener("click", () => { this.endChallenge(); this.app.replay.exit(); });
  }
}

const key = (x) => `${x.pattern}|${x.dir}|${x.points.map((p) => p.time).join(",")}`;
const same = (a, b) => !!a && !!b && key(a) === key(b);

function result(x) {
  const r = x.reaction;
  if (!r) return ` <span class="dim">· waiting</span>`;
  if (r.t1_hit) return ` <span class="up">· worked ✓</span>`;
  return r.complete ? ` <span class="down">· failed ✗</span>` : ` <span class="dim">· in progress</span>`;
}

function explain(it) {
  const forming = !!it.prz;
  const pts = it.points;
  const d = pts[pts.length - 1];
  const steps = it.checks.map((c) => `<li class="${c.info ? "dim" : c.ok ? "" : "down"}">${c.ok ? "✓" : c.info ? "·" : "✗"} ${esc(c.text)} <span class="dim">(${esc(it.pattern)} needs ${c.need})</span></li>`).join("");
  const r = it.reaction;
  const how = forming
    ? `<b>Plan:</b> wait for price to reach the D zone ${fmtPrice(it.prz[0])}–${fmtPrice(it.prz[1])}, then a reversal candle. ${it.status ? "Now: " + esc(it.status) + "." : ""}`
    : `<b>Trade idea:</b> ${it.dir === "bullish" ? "buy" : "sell"} on a reversal candle at D (${fmtPrice(d.price)}); stop beyond D; target 1 = 38.2% back toward A.`
      + (r ? ` <b>What happened:</b> price came back ${(r.retrace * 100).toFixed(0)}% of AD in the next ${r.bars} bars${r.t1_hit ? " — target 1 reached ✓" : ""}.` : "");
  return `<div class="co-head ${it.dir === "bullish" ? "up" : "down"}">${it.dir === "bullish" ? "▲ Bullish" : "▼ Bearish"} ${esc(it.pattern)}${forming ? " (forming)" : ""}</div>
    <div class="small dim">${pts.map((p) => `${p.label} ${fmtPrice(p.price)}`).join(" → ")}</div>
    <ul class="co-checks small">${steps}</ul><div class="small">${how}</div>`;
}
