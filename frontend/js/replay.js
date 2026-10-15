// Bar replay across all chart windows with ONE clock.
// The window you start from is the main window: you step it (1 minute or 1 bar), and every other window follows to the
// same moment — e.g. NIFTY 5m in one window and INDIA VIX in another always show the same time, never the future.
// Step modes:  "minute" — each step adds one minute; the current candle builds up like live (needs 1-minute data).
//              "bar"    — each step reveals a whole candle of the main window.
// Indicator values / markers / patterns appear only after their bar has closed (knownUntil rule).
import { api, store } from "./api.js";
import { TF_SECONDS, fmtTime, aggregate, bucketStart, anchorOf } from "./core.js";

const MIN_CHUNK = 8000;     // 1-minute bars per request (~21 sessions)
const BAR_CHUNK = 4000;

/** Replay state of one window. cursor = index of the last revealed element (−1 = nothing revealed yet). */
export class PanelReplay {
  constructor(panel, tf, minuteMode, bars, hist, anchor) {
    Object.assign(this, { panel, tf, minuteMode, bars, hist, anchor, cursor: -1, shown: null });
    this.times = bars.map((b) => b.time);
  }
  span() { return this.minuteMode ? 60 : TF_SECONDS[this.tf]; }
  knownAt(i) { return this.times[i] + this.span(); }               // data is known when the element has closed
  get known() { return this.cursor >= 0 ? this.knownAt(this.cursor) : null; }
  get current() { return this.cursor >= 0 ? this.bars[this.cursor] : null; }
  get atEnd() { return this.cursor >= this.bars.length - 1; }
  /** move to the last element that is known by time K */
  syncTo(K) {
    while (this.cursor + 1 < this.bars.length && this.knownAt(this.cursor + 1) <= K) this.cursor++;
    while (this.cursor >= 0 && this.knownAt(this.cursor) > K) this.cursor--;
  }
  visibleBars() {
    if (!this.minuteMode) return this.bars.slice(0, this.cursor + 1);
    return this.hist.concat(this.cursor >= 0 ? aggregate(this.bars, this.tf, this.cursor, this.anchor) : []);
  }
}

export class ReplayController {
  constructor(app) {
    this.app = app;
    this.rs = new Map();           // panel → PanelReplay
    this.master = null;
    this.mode = store.get("replayMode", "minute");
    this.active = false;
    this.picking = false;
    this.timer = null;
    this.speed = store.get("replaySpeed", 5);
    this.bar = document.getElementById("replay-bar");
    const $ = (s) => this.bar.querySelector(s);
    $(".rp-back").onclick = () => this.step(-1);
    $(".rp-fwd").onclick = () => this.step(1);
    $(".rp-close").onclick = () => this.toBarClose();
    $(".rp-play").onclick = () => this.togglePlay();
    $(".rp-exit").onclick = () => this.exit();
    const sp = $(".rp-speed"); sp.value = String(this.speed);
    sp.onchange = (e) => { this.speed = Number(e.target.value); store.set("replaySpeed", this.speed); if (this.timer) { this.pause(); this.play(); } };
    const md = $(".rp-mode"); md.value = this.mode;
    md.onchange = (e) => this.setMode(e.target.value);
    $(".rp-date").onchange = (e) => e.target.value && this.startAtDate(e.target.value);
    $(".rp-random").onclick = () => this.startRandom();
    $(".rp-blind").onclick = () => this.startBlind();
    $(".rp-reveal").onclick = () => this.reveal();
    this.blind = false;
  }

  /** Blind practice: a random day with every date hidden (axis, legend, status, news). Reveal at the end. */
  async startBlind() {
    const { first, last } = this.app.active.state;
    if (!first || !last) return;
    let t = first + Math.random() * (last - first - 86400 * 5);
    while ([0, 6].includes(new Date(t * 1000).getUTCDay())) t += 86400;
    this.setBlind(true);
    this.blindDate = new Date(t * 1000).toISOString().slice(0, 10);
    this.blindStartTrades = this.app.practice.done.length;
    await this.startAtDate(this.blindDate);
    this.app.toast("🙈 Blind mode: the date is hidden. Trade it like a live day, then press Reveal.");
  }

  setBlind(on) {
    this.blind = on;
    document.body.classList.toggle("blind", on);
    for (const p of this.app.panels) { p.view.setBlind(on); p.ohlcLegend({}); }
    this.app.news?.refresh(true);
  }

  reveal() {
    if (!this.blind) return;
    const done = this.app.practice.done.slice(this.blindStartTrades || 0);
    const net = done.reduce((a, t) => a + t.pnl_rs, 0);
    const d = new Date(this.blindDate + "T00:00:00Z").toUTCString().slice(0, 16);
    this.setBlind(false);
    this.app.toast(`Blind day was ${d} — ${done.length} trade${done.length === 1 ? "" : "s"}, ${net >= 0 ? "+" : "−"}₹${Math.abs(Math.round(net)).toLocaleString("en-IN")}`, net >= 0 ? "ok" : "error");
    this.status();
  }

  get knownUntil() { const m = this.rs.get(this.master); return m ? m.known : null; }
  get minuteMode() { return this.rs.get(this.master)?.minuteMode; }
  get model() { const m = this.rs.get(this.master); return m ? { time: m.current?.time, cursor: m.cursor, bars: m.bars } : null; }

  pick() {
    this.picking = true;
    document.body.classList.add("picking");
    this.bar.hidden = false;
    this.status("Click a bar (in any window) to start the replay there — or pick a date / 🎲 random day");
    this.app.toast("Replay: click the bar where you want to start, or choose a date in the replay bar");
  }

  onPick(panel, p) {
    if (!this.picking || !p || p.time == null) return;
    this.picking = false;
    document.body.classList.remove("picking");
    this.app.setActive(panel);
    this.start(panel, p.time);
  }

  /** start at the session open of day d (NSE 09:15; 24-hour markets 05:30 IST) */
  async startAtDate(d) {
    const a = anchorOf(this.app.symbols?.find((x) => x.symbol === this.app.active.state.symbol));
    await this.start(this.app.active, Math.floor(Date.parse(d + "T00:00:00Z") / 1000) + a * 60, true);
  }

  async startRandom() {
    const { first, last } = this.app.active.state;
    if (!first || !last) return;
    const t = first + Math.random() * (last - first - 86400 * 5);
    const d = new Date(t * 1000).toISOString().slice(0, 10);
    this.bar.querySelector(".rp-date").value = d;
    await this.startAtDate(d);
  }

  /** Load one window's replay data around time t (a time inside the bar to start from). */
  async loadPanel(panel, t, minute) {
    const { symbol, tf } = panel.state;
    const anchor = anchorOf(this.app.symbols?.find((x) => x.symbol === symbol));
    if (minute) {
      const t0 = bucketStart(t, tf, anchor);
      const [hist, mins] = await Promise.all([
        api.candles({ symbol, tf, before: t0, limit: 1500 }),
        api.candles({ symbol, tf: "1", start: t0, limit: MIN_CHUNK }),
      ]);
      if (mins.bars.length && mins.bars[0].time - t0 < TF_SECONDS[tf] + 86400 * 4) return new PanelReplay(panel, tf, true, mins.bars, hist.bars, anchor);
    }
    const [hist, fut] = await Promise.all([
      api.candles({ symbol, tf, before: t + 1, limit: 1500 }),
      api.candles({ symbol, tf, start: t + 1, limit: BAR_CHUNK }),
    ]);
    const r = new PanelReplay(panel, tf, false, hist.bars.concat(fut.bars), [], anchor);
    r.histLen = hist.bars.length;
    return r;
  }

  /** Start replaying with `master` as the main window at its bar time `t`.
   *  keepK: restart at exactly this moment (timeframe / mode / symbol change). */
  async start(master, t, dayStart = false, keepK = null) {
    this.pause();
    this.master = master;
    const wantMinute = this.mode === "minute";
    const m = await this.loadPanel(master, keepK != null ? keepK - 60 : t, wantMinute);
    if (!m.bars.length) { this.app.toast("No data at that date", "error"); return; }
    if (wantMinute && !m.minuteMode) this.app.toast("No 1-minute data for that date — replaying whole bars instead", "error");
    if (keepK != null) m.syncTo(keepK);
    else if (m.minuteMode) m.cursor = 0;                                          // the candle starts forming
    else if (dayStart) { const i = m.bars.findIndex((b) => b.time >= t); m.cursor = i >= 0 ? i : m.bars.length - 1; }
    else m.cursor = Math.max(0, (m.histLen ?? 1) - 1);                           // the clicked bar is the last visible
    if (m.cursor < 0) m.cursor = 0;
    this.rs = new Map([[master, m]]);
    const K = m.known;
    await Promise.all(this.app.panels.filter((p) => p !== master).map(async (p) => {
      const r = await this.loadPanel(p, K - 60, wantMinute);
      r.syncTo(K);
      this.rs.set(p, r);
    }));
    this.active = true;
    this.bar.hidden = false;
    this.app.enterReplay();
    for (const r of this.rs.values()) this.enterPanel(r);
    this.renderAll(true);
    this.app.news?.ensureRange(m.bars[0]?.time, m.bars[m.bars.length - 1]?.time, m.current?.time);
    await Promise.all([...this.rs.values()].map((r) => r.panel.indicators.refresh()));
    this.renderAll(true);
  }

  enterPanel(r) {
    const first = r.minuteMode ? (r.hist[0]?.time ?? r.bars[0]?.time) : r.bars[0]?.time;
    r.panel.enterReplay(first, r.bars[r.bars.length - 1]?.time);
  }

  async setMode(mode) {
    this.mode = mode;
    this.bar.querySelector(".rp-mode").value = mode;
    store.set("replayMode", mode);
    if (this.active) await this.start(this.master, null, false, this.knownUntil);
  }

  /** a window changed symbol or timeframe during replay: reload it at the current moment */
  async panelChanged(panel) {
    if (!this.active) return panel.load();
    const K = this.knownUntil;
    if (panel === this.master) {
      if (this.app.practice.engine.pos) this.app.practice.closeAll("symbol / timeframe change");
      this.app.practice.symbolChanged(this.app.symbols.find((x) => x.symbol === panel.state.symbol));
      panel.indicators.clearAll();
      panel.el.querySelector(".p-title").textContent = panel.state.symbol;
      await this.start(panel, null, false, K);
      panel.view.autoScale();
      return;
    }
    panel.indicators.clearAll();
    const r = await this.loadPanel(panel, K - 60, this.mode === "minute");
    r.syncTo(K);
    this.rs.set(panel, r);
    panel.el.querySelector(".p-title").textContent = panel.state.symbol;
    this.enterPanel(r);
    this.renderPanel(r, true, K);
    panel.view.autoScale();
    await panel.indicators.refresh();
    this.renderPanel(r, true, K);
  }

  async step(n) {
    if (!this.active || this._stepping) return;
    this._stepping = true;
    try {
      const m = this.rs.get(this.master);
      if (n > 0 && m.cursor + n > m.bars.length - 1) await this.extend(m);
      const before = m.cursor;
      m.cursor = Math.max(0, Math.min(m.bars.length - 1, m.cursor + n));
      if (m.cursor === before) { if (m.atEnd) { this.pause(); this.status(); } return; }
      if (n > 0) for (let i = before + 1; i <= m.cursor; i++) this.app.practice.onBar(m.bars[i], m.span());
      if (n > 0) this.checkAlerts(m, before);
      const K = m.known;
      const jobs = [[m, n !== 1]];
      for (const r of this.rs.values()) {
        if (r === m) continue;
        const b = r.cursor;
        r.syncTo(K);
        if (r.cursor > b) this.checkAlerts(r, b);
        if (r.atEnd && r.bars.length) this.extend(r).then(() => { r.syncTo(this.knownUntil); this.renderPanel(r, true, this.knownUntil); });
        jobs.push([r, r.cursor - b !== 1]);
      }
      for (const [r, full] of jobs) {
        if (r !== m && r.cursor === r.shown) { r.panel.view.knownUntil = K; continue; }   // nothing new in that window
        this.renderPanel(r, full, K);
        r.panel.view.followLast();
      }
      this.afterRender();
    } finally { this._stepping = false; }
  }

  /** line alerts for the elements revealed after index `from` */
  checkAlerts(r, from) {
    for (let i = Math.max(1, from + 1); i <= r.cursor; i++) this.app.alerts.checkLines(r.panel, r.bars[i - 1].close, r.bars[i]);
  }

  /** minute mode: jump to the close of the main window's forming candle */
  async toBarClose() {
    const m = this.rs.get(this.master);
    if (!m) return;
    if (!m.minuteMode) return this.step(1);
    const k = bucketStart(m.current.time, m.tf, m.anchor);
    let j = m.cursor;
    while (j + 1 < m.bars.length && bucketStart(m.bars[j + 1].time, m.tf, m.anchor) === k) j++;
    if (j === m.cursor) j = Math.min(m.bars.length - 1, j + 1);
    await this.step(j - m.cursor);
  }

  /** fetch the next chunk for a window that reached the end of its loaded data */
  async extend(r) {
    if (r._ext) return r._ext;
    if (r._noMore) return;
    const { symbol } = r.panel.state;
    const last = r.bars[r.bars.length - 1]?.time;
    if (last == null) return;
    r._ext = (async () => {
      const res = await api.candles({ symbol, tf: r.minuteMode ? "1" : r.tf, start: last + 1, limit: r.minuteMode ? MIN_CHUNK : BAR_CHUNK });
      if (res.bars.length) {
        r.bars.push(...res.bars);
        r.times.push(...res.bars.map((b) => b.time));
        r.panel.state.end = res.bars[res.bars.length - 1].time;
        r.panel.indicators.refresh().then(() => this.renderPanel(r, true, this.knownUntil));
      } else r._noMore = true;
    })();
    try { await r._ext; } finally { r._ext = null; }
  }

  renderAll(full) { const K = this.knownUntil; for (const r of this.rs.values()) this.renderPanel(r, full, K); this.afterRender(); }

  renderPanel(r, full, K) {
    const v = r.panel.view;
    v.knownUntil = K;
    const cur = r.current;
    if (full || r.shown == null || !cur || r.cursor < r.shown) v.setBars(r.visibleBars(), r.tf, { keepView: v.bars.length > 0 });
    else if (!r.minuteMode) v.pushBar(cur);
    else {
      const k = bucketStart(cur.time, r.tf, r.anchor);
      const last = v.bars[v.bars.length - 1];
      if (last && last.time === k) v.pushBar({ ...last, high: Math.max(last.high, cur.high), low: Math.min(last.low, cur.low), close: cur.close });
      else v.pushBar({ time: k, open: cur.open, high: cur.high, low: cur.low, close: cur.close, volume: cur.volume || 0 });
    }
    r.shown = r.cursor;
    r.panel.indicators.applyCursor(full);
    r.panel.ohlcLegend({});
    v.requestDraw();
  }

  afterRender() {
    const m = this.rs.get(this.master);
    if (m?.current) this.app.practice.setPrice(m.current.close, m.current.time, m.known);
    this.app.news?.onClock(m?.known);
    this.app.coach?.onClock();
    this.app.lab?.onClock();
    this.status();
  }

  status(text) {
    const el = this.bar.querySelector(".rp-status");
    if (text) { el.textContent = text; return; }
    const m = this.rs.get(this.master);
    if (!m || !m.current) return;
    const others = this.app.panels.length > 1 ? `  ·  ${this.app.panels.length} windows in sync` : "";
    el.textContent = `${this.blind ? "🙈 " + fmtTime(m.current.time).slice(-5) : fmtTime(m.current.time)}  ·  ${m.minuteMode ? "minute" : "bar"} ${m.cursor + 1}/${m.bars.length}${m.atEnd ? " · end of data" : ""}${others}`;
    this.bar.querySelector(".rp-play").textContent = this.timer ? "⏸" : "▶";
  }

  play() {
    if (!this.active || this.timer) return;
    this.timer = setInterval(() => this.step(1), 1000 / this.speed);
    this.bar.querySelector(".rp-play").textContent = "⏸";
  }
  pause() { if (this.timer) clearInterval(this.timer); this.timer = null; this.bar.querySelector(".rp-play").textContent = "▶"; }
  togglePlay() { this.timer ? this.pause() : this.play(); }

  exit(reload = true) {
    this.pause();
    if (this.blind) this.reveal();
    const was = this.active || this.picking;
    this.active = false;
    this.picking = false;
    this.rs = new Map();
    this.master = null;
    this.bar.hidden = true;
    document.body.classList.remove("picking");
    if (was) this.app.exitReplay(reload);
  }
}
