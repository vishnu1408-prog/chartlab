// One chart window: its own symbol, timeframe, indicators, drawings and live feed.
// The App owns several panels (split screen); the replay controller drives all of them from one clock.
import { api } from "./api.js";
import { ChartView } from "./chart.js";
import { Drawings } from "./drawings.js";
import { IndicatorManager, esc } from "./indicators.js";
import { fmtPrice, fmtTime, mcxCloseMin } from "./core.js";

const DEFAULT_INDICATORS = [{ uid: "h1", id: "harmonic", params: {}, visible: true }];

export class ChartPanel {
  constructor(app, cfg, host) {
    this.app = app;
    this.state = { symbol: cfg.symbol || "NIFTY 50", tf: cfg.tf || "15", chartType: cfg.chartType === "ha" ? "ha" : "candles", first: null, last: null, hasMore: false, start: null, end: null };
    this.el = document.createElement("div");
    this.el.className = "panel";
    this.el.innerHTML = `
      <div class="legend"><div><b class="p-title"></b> <span class="p-ohlc"></span></div><div class="legend-ind"></div></div>
      <div class="chart"></div>
      <button class="btn-auto on" title="Auto-fit the price axis (double-click the price axis does the same)">A</button>`;
    host.appendChild(this.el);
    this.view = new ChartView(this.el.querySelector(".chart"));
    this.view.setChartType(this.state.chartType);
    this.indicators = new IndicatorManager(this.view, this.el.querySelector(".legend-ind"), {
      toast: (m, k) => app.toast(m, k), active: cfg.indicators ?? DEFAULT_INDICATORS.map((x) => ({ ...x })), onSave: () => app.saveLayout(),
      onNewMarker: (a, name, m) => app.alerts.indicator(this, a, name, m), onBacktest: (a) => app.backtest.open(this, a),
    });
    this.indicators.context = this.state;
    this.drawings = new Drawings(this.view, {
      onChange: (items) => app.saveDrawings(this, items), onSelect: (it, edit) => app.selectDrawing(this, it, edit),
      onTool: (t) => app.markTool(this, t),
    });
    this.loadingOlder = false;
    this.ws = null;
    this._loaded = null;
    const ch = this.view.chart;
    ch.subscribeCrosshairMove((p) => { this.ohlcLegend(p); this.markAuto(); app.syncCrosshair(this, p); });
    ch.timeScale().subscribeVisibleLogicalRangeChange((r) => { this.maybeLoadOlder(r); app.syncRange(this); });
    ch.subscribeClick((p) => app.replay.onPick(this, p));
    this.el.addEventListener("mousedown", () => app.setActive(this), true);
    this.el.querySelector(".btn-auto").onclick = () => { this.view.autoScale(); this.markAuto(); };
    app.practice.attachDrag(this);
    app.posLines.attach(this);
  }

  config() { return { symbol: this.state.symbol, tf: this.state.tf, chartType: this.state.chartType, indicators: this.indicators.config() }; }

  markAuto() {
    const on = this.view.isAutoScale, b = this.el.querySelector(".btn-auto");
    if (b.classList.contains("on") !== on) b.classList.toggle("on", on);
  }

  async load() {
    const { symbol, tf } = this.state;
    this.el.querySelector(".p-title").textContent = symbol;
    const r = await api.candles({ symbol, tf, limit: tf === "1" ? 3000 : 2000 });
    Object.assign(this.state, { first: r.first, last: r.last, hasMore: r.has_more, start: null, end: null });
    this.view.knownUntil = null;
    const changed = this._loaded !== symbol;
    if (changed) this.indicators.clearAll();           // old symbol's lines would stretch the new price scale
    const sy = this.app.symbols?.find((x) => x.symbol === symbol);
    this.view.session = { start: sy?.session_start ?? 555, end: sy?.exchange === "MCX" ? mcxCloseMin : sy?.session_end ?? 930 };
    this.view.setBars(r.bars, tf);
    if (changed) this.view.autoScale();                // new symbol: always start at its own price level
    this._loaded = symbol;
    this.view.chart.timeScale().scrollToRealTime();
    this.markAuto();
    if (this.app.active === this && this.app.chain?.visible && this.app.chain.symbol !== symbol) this.app.chain.refresh(true);
    if (this.app.tradePanel === this) {
      this.app.practice.symbolChanged(this.app.symbols.find((x) => x.symbol === symbol));
      if (r.bars.length) this.app.practice.setPrice(r.bars[r.bars.length - 1].close, r.bars[r.bars.length - 1].time);
    }
    this.app.posLines.draw(this);                       // open paper positions on this symbol / option
    this.drawings.setItems(await api.drawings(symbol).catch(() => []));
    await this.indicators.refresh();
    this.ohlcLegend({});
    this.connectLive();
    if (this.app.active === this) { this.app.coach.refresh(this); this.app.health(); }
  }

  async maybeLoadOlder(range) {
    if (!range || this.loadingOlder || this.app.replay.active || !this.state.hasMore || range.from > 30) return;
    this.loadingOlder = true;
    try {
      const v = this.view;
      const { symbol, tf } = this.state;
      const r = await api.candles({ symbol, tf, before: v.bars[0].time, limit: 2000 });
      if (r.bars.length) {
        const cur = v.chart.timeScale().getVisibleLogicalRange();
        v.setBars(r.bars.concat(v.bars), tf);
        if (cur) v.chart.timeScale().setVisibleLogicalRange({ from: cur.from + r.bars.length, to: cur.to + r.bars.length });
        await this.indicators.refresh();
      }
      this.state.hasMore = r.has_more;
    } finally { this.loadingOlder = false; }
  }

  closeLive() { if (this.ws) { this.ws.onclose = null; this.ws.close(); this.ws = null; } }

  connectLive() {
    this.closeLive();
    if (this.app.replay.active) return;
    const { symbol, tf } = this.state;
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/live?symbol=${encodeURIComponent(symbol)}&tf=${tf}`);
    this.ws = ws;
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type !== "bars" || m.symbol !== this.state.symbol || m.tf !== this.state.tf || this.app.replay.active) return;
      const v = this.view;
      const lastT = v.bars.length ? v.bars[v.bars.length - 1].time : 0;
      // the feed only carries the latest few candles: if they start after a gap (page open across the night / a
      // holiday, or the feed caught up late), reload so the missing candles appear instead of a hole
      if (lastT && m.bars.length && m.bars[0].time > lastT + v.tfSec && !this._gapReload) {
        this._gapReload = true;
        this.load().finally(() => setTimeout(() => (this._gapReload = false), 30000));
        return;
      }
      let newBar = false;
      const prevClose = v.bars.length ? v.bars[v.bars.length - 1].close : null;
      for (const b of m.bars) if (b.time >= lastT) { if (b.time > lastT) newBar = true; v.pushBar(b); }
      const last = v.bars[v.bars.length - 1];
      if (last && prevClose != null) this.app.alerts.checkLines(this, prevClose, { ...last, high: Math.max(last.close, prevClose), low: Math.min(last.close, prevClose) });
      if (last && this.app.tradePanel === this) {
        // live: stops / targets are checked against the latest price only (the forming bar's earlier range is past)
        const px = last.close;
        this.app.practice.onBar({ time: last.time, open: px, high: px, low: px, close: px, tick: true });
        this.app.practice.setPrice(px, last.time);
      }
      if (newBar) { this.indicators.refresh(); if (this.app.active === this) this.app.coach.onClock(); }
      this.ohlcLegend({});
    };
    ws.onclose = () => { if (this.ws === ws) setTimeout(() => this.connectLive(), 5000); };
    const ping = setInterval(() => (ws.readyState === 1 ? ws.send("ping") : ws.readyState > 1 && clearInterval(ping)), 25000);
  }

  enterReplay(start, end) {
    this.closeLive();
    this.state.start = start; this.state.end = end;
    this.indicators.replay = true;
    this.indicators.lastSeen = {};
  }

  ohlcLegend(p) {
    const v = this.view;
    let b = p && p.seriesData ? p.seriesData.get(v.candles) : null;
    if (!b) b = v.bars[v.bars.length - 1];
    const el = this.el.querySelector(".p-ohlc");
    if (!b) { el.textContent = ""; return; }
    const real = v.chartType === "ha" ? v.barAt(b.time) : null;
    if (real && !(p && p.seriesData)) b = v.shown[v.shown.length - 1] || b;
    const ch = b.close - b.open;
    const tag = real ? `<span class="tag-ha" title="Heikin Ashi values are averages, not traded prices">HA</span> ` : "";
    const px = real ? ` <span class="dim">· price</span> <b>${fmtPrice(real.close)}</b>` : "";
    el.innerHTML = `${tag}<span class="dim">${this.app.replay.blind ? fmtTime(b.time).slice(-5) : fmtTime(b.time)}</span> O <b>${fmtPrice(b.open)}</b> H <b>${fmtPrice(b.high)}</b> L <b>${fmtPrice(b.low)}</b>
      C <b class="${ch >= 0 ? "up" : "down"}">${fmtPrice(b.close)}</b> <span class="${ch >= 0 ? "up" : "down"}">${ch >= 0 ? "+" : ""}${fmtPrice(ch)}</span>${px}`;
  }

  destroy() {
    this.closeLive();
    clearInterval(this.view._cdTimer);
    try { this.view.chart.remove(); } catch { /* already removed */ }
    this.el.remove();
  }
}

export { esc };
