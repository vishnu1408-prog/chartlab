import { api, store } from "./api.js";
import { TOOLS } from "./drawings.js";
import { FIB_EXT_LEVELS, parseLevels } from "./core.js";
import { esc } from "./indicators.js";
import { ChartPanel } from "./panel.js";
import { ReplayController } from "./replay.js";
import { PracticePanel } from "./practice.js";
import { NewsPanel } from "./news.js";
import { ChainPanel } from "./chain.js";
import { PaperPanel } from "./paper.js";
import { PositionLines } from "./poslines.js";
import { StatsDialog } from "./stats.js";
import { AlertManager } from "./alerts.js";
import { BacktestDialog } from "./backtest.js";
import { PatternCoach } from "./coach.js";
import { HarmonicLab } from "./lab.js";
import { TF_SECONDS, PATTERNS, optionChartName } from "./core.js";

const $ = (s) => document.querySelector(s);
const TFS = [["1", "1m"], ["3", "3m"], ["5", "5m"], ["15", "15m"], ["30", "30m"], ["60", "1h"], ["D", "1D"]];
export const LAYOUTS = { "1": 1, "2v": 2, "2h": 2, "4": 4 };
const PARTNERS = ["INDIA VIX", "NIFTY BANK", "SENSEX"];      // what a new window shows by default

class App {
  constructor() {
    this.symbols = [];
    this.catalog = { indicators: [], errors: [] };
    this.panels = [];
    this.active = null;
    this.layoutMode = "1";
    this.link = store.get("link", true);         // crosshair + time scroll linked across windows
    this.magnet = store.get("magnet", true);
    this.stayInTool = false;
    this.tool = "cursor";
    this.alerts = new AlertManager(this);
    this.backtest = new BacktestDialog(this);
    this.stats = new StatsDialog(this);
    this.replay = new ReplayController(this);
    this.practice = new PracticePanel(this);
    this.posLines = new PositionLines(this);
    this.news = new NewsPanel(this);
    this.chain = new ChainPanel(this);
    this.paper = new PaperPanel(this);
    this.coach = new PatternCoach(this);
    this.lab = new HarmonicLab(this);
    this.buildToolbar();
    this.bindKeys();
  }

  // the practice panel trades the replay's main window during replay, else the selected window
  get tradePanel() { return this.replay.active && this.replay.master ? this.replay.master : this.active; }
  // compatibility: "the" chart = the selected window
  get view() { return this.active.view; }
  get state() { return this.active.state; }
  get indicators() { return this.active.indicators; }
  get drawings() { return this.active.drawings; }

  async init() {
    try {
      const [syms, cat] = await Promise.all([api.symbols(), api.indicators()]);
      this.symbols = syms;
      this.catalog = cat;
      $("#symbols").innerHTML = syms.map((s) => `<option value="${esc(s.symbol)}">${esc(s.note || s.kind)}</option>`).join("");
      // saved layout (migrates the single-chart settings of the first version)
      const saved = store.get("layout", null) || { mode: "1", panels: [{ symbol: store.get("symbol", "NIFTY 50"), tf: store.get("tf", "15"), indicators: store.get("indicators", undefined) }] };
      this.panelCfgs = saved.panels || [];
      await this.setLayout(saved.mode in LAYOUTS ? saved.mode : "1", saved.active || 0);
      const want = new URLSearchParams(location.search).get("symbol");      // e.g. opened from the screener
      if (want && want !== this.active?.state.symbol) await this.setSymbol(want);
      this.news.refresh();
      this.health();
      setInterval(() => this.health(), 30000);
    } catch (e) { this.toast(`Startup failed: ${e.message}`, "error"); console.error(e); }
  }

  // ── layout / windows ──
  async setLayout(mode, activeIdx = null) {
    if (this.replay.active) this.replay.exit(false);
    const n = LAYOUTS[mode];
    const host = $("#panels");
    // keep existing windows, add / remove at the end
    this.panelCfgs = this.panels.length ? this.panels.map((p) => p.config()) : this.panelCfgs;
    while (this.panels.length > n) this.panels.pop().destroy();
    const added = [];
    while (this.panels.length < n) {
      const i = this.panels.length;
      const base = this.panels[0]?.state || { symbol: "NIFTY 50", tf: "15" };
      const cfg = this.panelCfgs[i] || { symbol: PARTNERS[(i - 1) % PARTNERS.length] || "NIFTY 50", tf: base.tf, indicators: [] };
      const p = new ChartPanel(this, cfg, host);
      p.indicators.setCatalog(this.catalog.indicators, this.catalog.errors);
      this.coach.attach(p);
      this.lab.attach(p);
      this.panels.push(p);
      added.push(p);
    }
    this.layoutMode = mode;
    host.className = "layout-" + mode;
    document.querySelectorAll("#layouts button").forEach((b) => b.classList.toggle("on", b.dataset.layout === mode));
    this.setActive(this.panels[Math.min(activeIdx ?? this.panels.indexOf(this.active), n - 1)] || this.panels[0]);
    await Promise.all(added.map((p) => p.load()));
    this.saveLayout();
  }

  setActive(p) {
    if (!p || this.active === p) return;
    const prevTool = this.tool;
    if (this.active) { this.active.el.classList.remove("active"); this.active.drawings.setTool("cursor"); this.selectDrawing(this.active, null); }
    this.active = p;
    p.el.classList.add("active");
    $("#symbol").value = p.state.symbol;
    if (this.chain?.visible) this.chain.refresh(true);
    this.markTf();
    p.drawings.magnet = this.magnet;
    p.drawings.stayInTool = this.stayInTool;
    p.drawings.lockAll = !!this.lockAll;
    if (prevTool !== "cursor") p.drawings.setTool(prevTool); else this.markTool(p, "cursor");
    if (!this.replay.active) {
      this.practice.symbolChanged(this.symbols.find((x) => x.symbol === p.state.symbol));
      const last = p.view.bars[p.view.bars.length - 1];
      if (last) this.practice.setPrice(last.close, last.time);
    }
    this.saveLayout();
    this.coach?.refresh(p);
  }

  saveLayout() {
    if (!this.panels.length) return;
    store.set("layout", { mode: this.layoutMode, active: Math.max(0, this.panels.indexOf(this.active)), panels: this.panels.map((p) => p.config()) });
  }

  // linked windows: same time under the crosshair, same time range when scrolling
  syncCrosshair(src, p) {
    if (!this.link || this._xsync || this.panels.length < 2) return;
    this._xsync = true;
    try {
      for (const q of this.panels) {
        if (q === src) continue;
        if (!p || p.time == null || !p.point) { q.view.chart.clearCrosshairPosition(); continue; }
        const i = q.view.times.length ? lastLEtime(q.view.times, p.time) : -1;
        if (i < 0) { q.view.chart.clearCrosshairPosition(); continue; }
        q.view.chart.setCrosshairPosition(q.view.bars[i].close, q.view.times[i], q.view.candles);
      }
    } finally { this._xsync = false; }
  }

  syncRange(src) {
    if (!this.link || this._rsync || this.panels.length < 2 || src !== this.active) return;
    const r = src.view.chart.timeScale().getVisibleRange();
    if (!r) return;
    this._rsync = true;
    try {
      for (const q of this.panels) {
        if (q === src || !q.view.bars.length) continue;
        try { q.view.chart.timeScale().setVisibleRange(r); } catch { /* range outside this window's data */ }
      }
    } finally { this._rsync = false; }
  }

  // ── toolbar ──
  buildToolbar() {
    const sym = $("#symbol");
    sym.onchange = () => {
      const s = sym.value.trim().toUpperCase();
      const up = (x) => x.symbol.toUpperCase();
      const hit = this.symbols.find((x) => up(x) === s) || this.symbols.find((x) => up(x).startsWith(s)) || this.symbols.find((x) => up(x).includes(s));
      if (/ (CE|PE)$/.test(s)) {                       // an option contract: "NIFTY 23100 CE" / "NIFTY 27-OCT-26 23100 CE"
        fetch(`/api/option/resolve?q=${encodeURIComponent(s)}`).then((r) => r.json()).then((r) => {
          if (r.symbol) this.setSymbol(r.symbol);
          else { this.toast(r.detail || `No option matches "${s}"`, "error"); sym.value = this.active?.state.symbol || ""; }
        });
        return;
      }
      if (hit) this.setSymbol(hit.symbol); else { this.toast(`No symbol matches "${s}"`, "error"); sym.value = this.active?.state.symbol || ""; }
    };
    sym.onkeydown = (e) => { if (e.key === "Enter") sym.blur(); };
    sym.onfocus = () => sym.select();
    const tfBox = $("#tfs");
    for (const [tf, lbl] of TFS) {
      const b = document.createElement("button"); b.textContent = lbl; b.dataset.tf = tf; b.onclick = () => this.setTf(tf); tfBox.appendChild(b);
    }
    document.querySelectorAll("#layouts button").forEach((b) => (b.onclick = () => this.setLayout(b.dataset.layout)));
    $("#btn-ctype").onclick = () => this.toggleChartType();
    const lk = $("#btn-link");
    lk.classList.toggle("on", this.link);
    lk.onclick = () => { this.link = !this.link; lk.classList.toggle("on", this.link); store.set("link", this.link); };
    $("#btn-ind").onclick = () => this.active.indicators.openPicker();
    $("#btn-replay").onclick = () => (this.replay.active ? this.replay.exit() : this.replay.pick());
    $("#btn-sync").onclick = () => this.sync();
    $("#btn-help").onclick = () => $("#dlg-help").showModal();
    $("#btn-practice").onclick = () => document.body.classList.toggle("no-side");
    const side = $("#practice");
    const showTab = (tab) => {
      side.dataset.tab = tab;
      side.querySelectorAll(".side-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
      store.set("sideTab", tab);
      if (tab === "news") this.news.refresh(true);
      if (tab === "chain") this.chain.refresh(true);
      if (tab === "paper") this.paper.refresh(true);
    };
    this.showTab = showTab;
    side.querySelectorAll(".side-tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
    showTab(store.get("sideTab", "practice"));
    $("#btn-stats").onclick = () => this.stats.open();
    $("#btn-coach").onclick = () => this.coach.toggle();
    this.markCoach();
    const tb = $("#tools");
    for (const t of TOOLS) {
      const b = document.createElement("button"); b.innerHTML = t.icon; b.title = t.title; b.dataset.tool = t.id;
      if (t.menu) { b.classList.add("has-menu"); b.onclick = (e) => { e.stopPropagation(); this.togglePatternMenu(b); }; }
      else b.onclick = () => this.setTool(t.id);
      tb.appendChild(b);
    }
    this.buildPatternMenu();
    const sep = document.createElement("div"); sep.className = "sep"; tb.appendChild(sep);
    for (const [icon, title, redo] of [["↶", "Undo drawing change (Ctrl+Z)", false], ["↷", "Redo (Ctrl+Y)", true]]) {
      const b = document.createElement("button"); b.textContent = icon; b.title = title; b.className = "undo-btn";
      b.onclick = () => { if (!(redo ? this.active.drawings.redo() : this.active.drawings.undo())) this.toast(redo ? "Nothing to redo" : "Nothing to undo"); };
      tb.appendChild(b);
    }
    const mag = document.createElement("button"); mag.innerHTML = "🧲"; mag.title = "Magnet: snap to open/high/low/close";
    mag.classList.toggle("on", this.magnet);
    mag.onclick = () => { this.magnet = !this.magnet; this.panels.forEach((p) => (p.drawings.magnet = this.magnet)); mag.classList.toggle("on", this.magnet); store.set("magnet", this.magnet); };
    tb.appendChild(mag);
    const lock = document.createElement("button"); lock.innerHTML = "📌"; lock.title = "Stay in drawing mode";
    lock.onclick = () => { this.stayInTool = !this.stayInTool; this.panels.forEach((p) => (p.drawings.stayInTool = this.stayInTool)); lock.classList.toggle("on", this.stayInTool); };
    tb.appendChild(lock);
    // 🔒 lock all drawings: they can't be moved by accident — dragging over a Fib / line pans the chart instead
    const lockAll = document.createElement("button"); lockAll.innerHTML = "🔒";
    lockAll.title = "Lock all drawings (dragging over them moves the chart). Lock one drawing: its settings → 🔒";
    this.lockAll = store.get("lockAll", false);
    const applyLock = () => { this.panels.forEach((p) => { p.drawings.lockAll = this.lockAll; p.view.requestDraw(); }); lockAll.classList.toggle("on", this.lockAll); };
    lockAll.onclick = () => { this.lockAll = !this.lockAll; store.set("lockAll", this.lockAll); applyLock(); this.toast(this.lockAll ? "🔒 All drawings locked" : "🔓 Drawings unlocked"); };
    applyLock();
    tb.appendChild(lockAll);
    const del = document.createElement("button"); del.innerHTML = "🗑"; del.title = "Delete all drawings on this symbol (selected window)";
    del.onclick = () => { if (confirm(`Delete all drawings on ${this.active.state.symbol}?`)) this.active.drawings.clear(); };
    tb.appendChild(del);
  }

  setTool(t) { this.tool = t; this.active.drawings.setTool(t); }
  markCoach() { $("#btn-coach")?.classList.toggle("on", !!this.coach?.on); }

  /** TradingView-style fly-out: chart patterns, Elliott waves, cycles */
  buildPatternMenu() {
    const m = $("#pattern-menu");
    const groups = {};
    for (const [id, p] of Object.entries(PATTERNS)) (groups[p.group] ||= []).push([id, p]);
    m.innerHTML = `<div class="pm-head">ChartLab coach</div>
      <button data-coach="auto">🤖 Auto-mark harmonic patterns <span class="pm-n">learn</span></button>
      <button data-coach="challenge">🏆 Pattern challenge: you vs ChartLab <span class="pm-n">play</span></button>`
      + Object.entries(groups).map(([g, list]) => `<div class="pm-head">${esc(g)}</div>` + list.map(([id, p]) =>
      `<button data-pat="${id}"><span class="pm-n">${p.labels.length} pts</span>${esc(p.name)}</button>`).join("")).join("");
    m.querySelector('[data-coach="auto"]').onclick = () => { m.hidden = true; this.coach.toggle(true); };
    m.querySelector('[data-coach="challenge"]').onclick = () => { m.hidden = true; this.coach.startChallenge(); };
    m.querySelectorAll("button[data-pat]").forEach((b) => (b.onclick = () => {
      m.hidden = true;
      this.setTool(b.dataset.pat);
      this.toast(`${PATTERNS[b.dataset.pat].name}: click ${PATTERNS[b.dataset.pat].labels.length} points on the chart (Esc cancels)`);
    }));
    document.addEventListener("mousedown", (e) => { if (!m.hidden && !m.contains(e.target) && !e.target.closest(".has-menu")) m.hidden = true; });
  }

  togglePatternMenu(btn) {
    const m = $("#pattern-menu");
    if (!m.hidden) { m.hidden = true; return; }
    const r = btn.getBoundingClientRect();
    m.style.top = `${Math.min(r.top, window.innerHeight - 460)}px`;
    m.style.left = `${r.right + 4}px`;
    m.querySelectorAll("button[data-pat]").forEach((b) => b.classList.toggle("on", b.dataset.pat === this.tool));
    m.hidden = false;
  }
  markTool(panel, t) {
    if (panel !== this.active) return;
    this.tool = t;
    document.querySelectorAll("#tools [data-tool]").forEach((b) => b.classList.toggle("on", b.dataset.tool === t || (b.dataset.tool === "patterns" && !!PATTERNS[t])));
    const pb = document.querySelector('#tools [data-tool="patterns"]');
    if (pb) pb.title = PATTERNS[t] ? `${PATTERNS[t].name} — click ${PATTERNS[t].labels.length} points` : TOOLS.find((x) => x.id === "patterns").title;
  }
  markTf() {
    document.querySelectorAll("#tfs button").forEach((b) => b.classList.toggle("on", b.dataset.tf === this.active?.state.tf));
    const ha = this.active?.state.chartType === "ha", b = $("#btn-ctype");
    if (b) { b.textContent = ha ? "🕯 Heikin Ashi" : "🕯 Candles"; b.classList.toggle("on", ha); }
  }

  /** candles ↔ Heikin Ashi for the selected window (kept in the saved layout) */
  toggleChartType(panel = this.active) {
    if (!panel) return;
    panel.state.chartType = panel.state.chartType === "ha" ? "candles" : "ha";
    panel.view.setChartType(panel.state.chartType);
    panel.ohlcLegend({});
    this.markTf();
    this.saveLayout();
    if (panel.state.chartType === "ha") this.toast("Heikin Ashi: candles are averaged, so they lag. The dotted line on the price axis is the real price — orders fill there.");
  }

  /** open an option contract's own chart (premium candles) in the active window */
  openOption({ symbol, expiry, strike, type }) {
    if (symbol === "BTC") { this.toast("BTC option charts are not available yet", "error"); return; }
    const name = optionChartName({ symbol, expiry, strike, type });
    this.setSymbol(name);
    this.toast(`📈 ${name} — the option's own price (premium) chart`);
  }

  async setSymbol(s, panel = this.active) {
    panel.state.symbol = s;
    if (panel === this.active) $("#symbol").value = s;
    this.saveLayout();
    if (this.replay.active) return this.replay.panelChanged(panel);
    await panel.load();
  }

  async setTf(tf, panel = this.active) {
    if (tf === panel.state.tf) return;
    panel.state.tf = tf;
    this.markTf();
    this.saveLayout();
    if (this.replay.active) return this.replay.panelChanged(panel);
    await panel.load();
  }

  async sync() {
    const symbol = this.active.state.symbol;
    this.toast(`Syncing ${symbol}…`);
    try {
      const r = await api.sync(symbol);
      const add = r.added ? Object.entries(r.added).map(([k, v]) => `${k}: ${v}`).join(", ") : r.status;
      this.toast(`Sync ${r.status}${add ? " — " + add : ""}`, r.status === "ok" ? "ok" : undefined);
      if (!this.replay.active) await Promise.all(this.panels.filter((p) => p.state.symbol === symbol).map((p) => p.load()));
    } catch (e) { this.toast(`Sync failed: ${e.message}`, "error"); }
  }

  async health() {
    try {
      const h = await api.health(this.active?.state.symbol);
      const el = $("#status");
      const live = this.panels.some((p) => p.ws && p.ws.readyState === 1);
      el.textContent = this.replay.active ? "REPLAY" : h.market_open ? ({ angel: "LIVE · Angel One", binance: "LIVE · Binance" }[h.source] || "LIVE · Yahoo (delayed)") : "Market closed";
      el.className = "status " + (this.replay.active ? "replay" : h.market_open && live ? "live" : "");
      el.title = h.detail || "";
    } catch { $("#status").textContent = "API offline"; }
  }

  // ── replay hooks (called by the replay controller) ──
  enterReplay() { document.body.classList.add("replaying"); this.health(); }
  async exitReplay(reload = true) {
    document.body.classList.remove("replaying");
    for (const p of this.panels) { p.indicators.replay = false; p.view.knownUntil = null; p.indicators.lastSeen = {}; delete p.view.markerSources.review; }
    if (reload) await Promise.all(this.panels.map((p) => p.load()));
    this.health();
  }

  // ── drawings ──
  saveDrawings(panel, items) {
    clearTimeout(panel._saveT);
    const symbol = panel.state.symbol;
    panel._saveT = setTimeout(() => api.saveDrawings(symbol, items)
      .then(() => this.panels.forEach((q) => { if (q !== panel && q.state.symbol === symbol) q.drawings.setItems(JSON.parse(JSON.stringify(items))); }))
      .catch((e) => this.toast(`Saving drawings failed: ${e.message}`, "error")), 300);
  }

  selectDrawing(panel, it, edit) {
    const pop = $("#draw-props");
    if (!it || panel !== this.active) { pop.hidden = true; return; }
    pop.hidden = false;
    pop.querySelector(".dp-color").value = it.color || "#2962ff";
    pop.querySelector(".dp-width").value = it.width || 2;
    const txt = pop.querySelector(".dp-text");
    txt.value = it.text || "";
    txt.parentElement.hidden = it.type !== "text";
    const rr = pop.querySelector(".dp-ratio");
    rr.value = it.ratio || 2;
    rr.parentElement.hidden = it.type !== "rr";
    const rows = pop.querySelector(".dp-rows");
    rows.value = it.rows || 24;
    rows.parentElement.hidden = it.type !== "frvp";
    const lk = pop.querySelector(".dp-lock");
    lk.checked = !!it.locked;
    const al = pop.querySelector(".dp-alert");
    al.checked = !!it.alert;
    al.parentElement.hidden = !["hline", "trend", "ray"].includes(it.type);
    const fib = pop.querySelector(".dp-fib");
    fib.hidden = it.type !== "fib" && it.type !== "fibext";
    // both Fib tools take their own levels; "Rev" only applies to the retracement
    pop.querySelector(".dp-fext").parentElement.hidden = true;
    pop.querySelector(".dp-frev").parentElement.hidden = it.type === "fibext";
    const flv = pop.querySelector(".dp-flv"), flevels = pop.querySelector(".dp-flevels");
    flv.hidden = it.type !== "fibext" && it.type !== "fib";
    flevels.value = (it.levels || FIB_EXT_LEVELS).join(", ");
    const fx = { ext: ".dp-fext", extend: ".dp-fextend", reverse: ".dp-frev", fill: ".dp-ffill" };
    for (const [k, sel] of Object.entries(fx)) pop.querySelector(sel).checked = k === "reverse" ? !!it.reverse : it[k] !== false;
    const chk = pop.querySelector(".dp-check");
    chk.hidden = !["xabcd", "cypher", "abcd"].includes(it.type);
    chk.onclick = () => this.lab.check(panel, it, this.lab.task?.kind === "mark" && this.lab.task.panel === panel ? this.lab.task.item : null);
    const upd = () => {
      it.color = pop.querySelector(".dp-color").value; it.width = Number(pop.querySelector(".dp-width").value);
      it.text = txt.value; it.ratio = Number(rr.value) || 2;
      if (it.type === "frvp") { it.rows = Math.max(5, Math.min(200, Number(rows.value) || 24)); panel.drawings.fitProfile(it); }
      if (al.checked && !it.alert) { this.alerts.arm(); this.toast("🔔 Alert set: you get a notification when price crosses this line"); }
      it.alert = al.checked;
      it.locked = lk.checked;
      if (it.type === "fib") for (const [k, sel] of Object.entries(fx)) it[k] = pop.querySelector(sel).checked;
      if (it.type === "fib") { const lv = parseLevels(flevels.value); if (lv) it.levels = lv; }
      if (it.type === "fibext") {
        it.extend = pop.querySelector(".dp-fextend").checked; it.fill = pop.querySelector(".dp-ffill").checked;
        const lv = parseLevels(flevels.value);
        if (lv) it.levels = lv;
      }
      panel.view.requestDraw(); panel.drawings.commit(); this.saveDrawings(panel, panel.drawings.items);
    };
    pop.querySelectorAll("input").forEach((i) => (i.oninput = upd));
    pop.querySelector(".dp-del").onclick = () => panel.drawings.remove(it);
    if (edit && it.type === "text") { txt.focus(); txt.select(); }
  }

  ohlcLegend() { for (const p of this.panels) p.ohlcLegend({}); }

  /** open a past trade (journal or backtest) in replay: the chart stops at the exit, with entry / exit marked */
  async reviewTrade(t, panel = this.active) {
    if (!t?.entry_time) return;
    this.setActive(panel);
    panel.view.setReviewLines([]);
    if (this.replay.active) this.replay.exit(false);
    if (t.symbol && t.symbol !== panel.state.symbol) {
      panel.state.symbol = t.symbol; $("#symbol").value = t.symbol;
      panel.el.querySelector(".p-title").textContent = t.symbol;      // the legend title follows the reviewed symbol
    }
    if (t.tf && t.tf !== panel.state.tf) { panel.state.tf = t.tf; this.markTf(); }
    this.saveLayout();
    const K = (t.exit_time ?? t.entry_time) + 60;
    await this.replay.start(panel, null, false, K);
    panel.view.autoScale();
    const long = String(t.side).includes("CE") || t.side === "BUY";
    panel.view.markerSources.review = [
      { time: t.entry_time, known: t.entry_time, position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown", color: "#e040fb", text: "entry" },
      ...(t.exit_time ? [{ time: t.exit_time, known: t.exit_time, position: long ? "aboveBar" : "belowBar", shape: "circle", color: "#e040fb",
                           text: `exit ${t.pnl_rs >= 0 ? "+" : ""}${Math.round(t.pnl_rs ?? 0)}` }] : []),
    ];
    panel.indicators.updateMarkers();
    panel.view.setReviewLines(t.levels || []);
    this.toast("Review: the chart stops at the exit. Step back (←) to see how the trade developed, or forward to see what happened next.");
  }

  // ── keys / toast ──
  bindKeys() {
    window.addEventListener("keydown", (e) => {
      if (e.target.matches("input, select, textarea") || document.querySelector("dialog[open]")) return;
      const k = e.key;
      if ((e.ctrlKey || e.metaKey) && (k.toLowerCase() === "z" || k.toLowerCase() === "y")) {   // drawings undo / redo
        const redo = k.toLowerCase() === "y" || e.shiftKey;
        const ok = redo ? this.active.drawings.redo() : this.active.drawings.undo();
        if (!ok) this.toast(redo ? "Nothing to redo" : "Nothing to undo");
        e.preventDefault();
        return;
      }
      if (this.replay.active) {
        if (k === "ArrowRight") { this.replay.step(e.shiftKey ? 10 : 1); e.preventDefault(); return; }
        if (k === "ArrowLeft") { this.replay.step(e.shiftKey ? -10 : -1); e.preventDefault(); return; }
        if (k === " ") { this.replay.togglePlay(); e.preventDefault(); return; }
        if (k === "ArrowDown") { this.replay.toBarClose(); e.preventDefault(); return; }
      }
      if (k === "Escape") { this.active.drawings.cancel(); this.setTool("cursor"); if (this.replay.picking) this.replay.exit(); }
      else if (k === "Delete" || k === "Backspace") this.active.drawings.remove();
      else if (e.altKey && k.toLowerCase() === "t") this.setTool("trend");
      else if (e.altKey && k.toLowerCase() === "h") this.setTool("hline");
      else if (e.altKey && k.toLowerCase() === "v") this.setTool("vline");
      else if (e.altKey && k.toLowerCase() === "r") this.setTool("rect");
      else if (e.altKey && k.toLowerCase() === "f") this.setTool("fib");
      else if (e.altKey && k.toLowerCase() === "e") this.setTool("fibext");
      else if (e.altKey && k.toLowerCase() === "p") this.setTool("pen");
      else if (e.altKey && k.toLowerCase() === "k") this.toggleChartType();
      else if (k.toLowerCase() === "b" && e.shiftKey) this.practice.order(1);
      else if (k.toLowerCase() === "s" && e.shiftKey) this.practice.order(-1);
      else if (k.toLowerCase() === "c" && e.shiftKey) this.practice.closeAll();
      else if (k === "/") { $("#symbol").focus(); e.preventDefault(); }
      else if (k === "Tab" && this.panels.length > 1) { this.setActive(this.panels[(this.panels.indexOf(this.active) + 1) % this.panels.length]); e.preventDefault(); }
      else if (/^[1-7]$/.test(k) && !e.ctrlKey && !e.metaKey && !e.altKey) this.setTf(TFS[Number(k) - 1][0]);
    });
  }

  toast(msg, kind = "") {
    const t = document.createElement("div");
    t.className = "toast " + kind;
    t.textContent = msg;
    $("#toasts").appendChild(t);
    setTimeout(() => t.remove(), kind === "error" ? 6000 : 3500);
  }
}

function lastLEtime(times, t) {
  let lo = 0, hi = times.length - 1, ans = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (times[m] <= t) { ans = m; lo = m + 1; } else hi = m - 1; }
  return ans;
}

window.chartlab = new App();
window.chartlab.init();
export { TF_SECONDS };
