// Chart view: lightweight-charts (candles, indicator series in panes, markers) + a canvas overlay for drawings and
// indicator shapes. Coordinates: anchors are {time, price}; time → fractional bar index → x.
import { TF_SECONDS, candleCloseAt, fmtCountdown, haBar, heikinAshi, timeToLogical, logicalToTime, clipPoints, visibleAt, fmtPrice, fmtTime } from "./core.js";

const LC = window.LightweightCharts;
export const COLORS = { bg: "#131722", grid: "#1e222d", text: "#d1d4dc", up: "#26a69a", down: "#ef5350" };

export class ChartView {
  constructor(el) {
    this.el = el;
    this.chart = LC.createChart(el, {
      autoSize: true,
      layout: { background: { type: "solid", color: COLORS.bg }, textColor: COLORS.text, fontFamily: "Inter, system-ui, sans-serif",
                panes: { separatorColor: "#2a2e39", enableResize: true } },
      grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
      crosshair: { mode: LC.CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#2a2e39" },
      timeScale: { borderColor: "#2a2e39", timeVisible: true, secondsVisible: false, rightOffset: 12, barSpacing: 8 },
      localization: { priceFormatter: (p) => fmtPrice(p), timeFormatter: (t) => this.crossTime(t) },
    });
    this.blind = false;
    this.chart.applyOptions({ timeScale: { tickMarkFormatter: (t, type) => this.tick(t, type) } });
    this.candles = this.chart.addSeries(LC.CandlestickSeries, {
      upColor: COLORS.up, downColor: COLORS.down, borderVisible: false, wickUpColor: COLORS.up, wickDownColor: COLORS.down,
    });
    this.markerApi = LC.createSeriesMarkers(this.candles, []);
    this.bars = [];                  // the real OHLC (practice fills, indicators, volume profile, replay all use these)
    this.shown = [];                 // what is drawn: the real candles, or Heikin Ashi built from them
    this.chartType = "candles";      // "candles" | "ha"
    this.realLine = null;            // HA mode: the real last price on the axis (an HA close is not a traded price)
    this.times = [];
    this.tf = "5";
    this.knownUntil = null;          // replay: end (seconds) of the data revealed so far; null = normal / live chart
    this.layers = [];                // functions (ctx, view) → void, drawn on the overlay in order
    this.priceLines = [];
    this.markerSources = {};         // markers from outside the indicators, by source: news, practice trades

    this.canvas = document.createElement("canvas");
    this.canvas.className = "overlay";
    el.appendChild(this.canvas);
    this.ctx = this.canvas.getContext("2d");
    // time left until the last candle closes, under the last-price label on the price axis (like TradingView)
    this.session = { start: 555, end: 930 };   // the symbol's session (minutes of the IST day); set by the panel
    this.countdown = document.createElement("div");
    this.countdown.className = "countdown";
    el.appendChild(this.countdown);
    this._cdTimer = setInterval(() => this.updateCountdown(), 1000);
    const redraw = () => this.requestDraw();
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(redraw);
    this.chart.subscribeCrosshairMove(redraw);
    new ResizeObserver(redraw).observe(el);
  }

  get tfSec() { return TF_SECONDS[this.tf] || 300; }

  setBars(bars, tf, { keepView = false } = {}) {
    const range = keepView ? this.chart.timeScale().getVisibleLogicalRange() : null;
    this.tf = tf;
    this.bars = bars;
    this.times = bars.map((b) => b.time);
    this.shown = this.chartType === "ha" ? heikinAshi(bars) : bars;
    this.candles.setData(this.shown);
    this.markRealPrice();
    if (range) this.chart.timeScale().setVisibleLogicalRange(range);
    this.requestDraw();
  }

  /** replay step: if the newest candle is off screen (scrolled away, or stepped back past the edge), slide the view
   *  so it sits near the right edge again; the zoom (bars per screen) stays the same. */
  followLast() {
    const ts = this.chart.timeScale(), r = ts.getVisibleLogicalRange(), last = this.bars.length - 1;
    if (!r || last < 0 || (last >= r.from && last <= r.to - 1)) return;
    const w = r.to - r.from, pad = Math.min(5, w * 0.1);
    ts.setVisibleLogicalRange({ from: last + pad - w, to: last + pad });
  }

  /** new replay start (random day / date / picked bar): put the newest candle at the right edge (same zoom) and
   *  fit the price axis again, so the new day is on screen even if the old view was scrolled or the axis dragged. */
  focusLast() {
    const ts = this.chart.timeScale(), r = ts.getVisibleLogicalRange(), last = this.bars.length - 1;
    if (last < 0) return;
    const w = r ? Math.max(20, r.to - r.from) : 120, pad = Math.min(5, w * 0.1);
    ts.setVisibleLogicalRange({ from: last + pad - w, to: last + pad });
    this.autoScale();
  }

  /** forward step / live tick: update or append the last bar without re-sending everything */
  pushBar(bar) {
    const n = this.bars.length;
    if (n && bar.time < this.bars[n - 1].time) return;
    const same = n && bar.time === this.bars[n - 1].time;
    if (same) this.bars[n - 1] = bar;
    else { this.bars.push(bar); this.times.push(bar.time); }
    let shown = bar;
    if (this.chartType === "ha") {                     // the forming bar is rebuilt from the HA bar before it
      if (same) this.shown.pop();
      shown = haBar(bar, this.shown[this.shown.length - 1]);
      this.shown.push(shown);
    } else this.shown = this.bars;
    this.candles.update(shown);
    this.markRealPrice();
    this.requestDraw();
  }

  /** "candles" or "ha" (Heikin Ashi). Only the drawing changes; prices you trade at stay the real ones. */
  setChartType(type) {
    const t = type === "ha" ? "ha" : "candles";
    if (t === this.chartType) return;
    this.chartType = t;
    this.candles.applyOptions({ lastValueVisible: t !== "ha", priceLineVisible: t !== "ha" });
    if (this.bars.length) this.setBars(this.bars, this.tf, { keepView: true });
    else this.markRealPrice();
  }

  /** shown bar at a time (for the legend / magnet: what you see on the chart) */
  shownAt(t) { const i = this.times.indexOf(t); return i >= 0 ? this.shown[i] : null; }

  markRealPrice() {
    const last = this.bars[this.bars.length - 1];
    if (this.chartType !== "ha" || !last) {
      if (this.realLine) { this.candles.removePriceLine(this.realLine); this.realLine = null; }
      return;
    }
    const color = last.close >= last.open ? COLORS.up : COLORS.down;
    const opts = { price: last.close, color, lineWidth: 1, lineStyle: LC.LineStyle.Dotted, axisLabelVisible: true, title: "" };
    if (this.realLine) this.realLine.applyOptions(opts); else this.realLine = this.candles.createPriceLine(opts);
  }

  setMarkers(markers) { this.markerApi.setMarkers(markers); }
  get extraMarkers() { return Object.values(this.markerSources).flat(); }

  /** fit the price axis to the visible candles again (after the user dragged / zoomed it) */
  autoScale() {
    this.chart.priceScale("right").applyOptions({ autoScale: true });
    this.chart.panes().forEach((p, i) => { if (i > 0) try { this.chart.priceScale("right", i).applyOptions({ autoScale: true }); } catch { /* pane without scale */ } });
  }

  get isAutoScale() { return this.chart.priceScale("right").options().autoScale; }

  /** Blind practice: time axis and crosshair show only the time of day — no dates. */
  setBlind(on) {
    this.blind = on;
    this.chart.applyOptions({ timeScale: { tickMarkFormatter: (t, type) => this.tick(t, type) },
                              localization: { priceFormatter: (p) => fmtPrice(p), timeFormatter: (t) => this.crossTime(t) } });
  }

  tick(t, type) {
    const d = new Date(t * 1000), p = (n) => String(n).padStart(2, "0");
    const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    if (this.blind) return type <= 2 ? "·" : `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
    if (type === 0) return String(d.getUTCFullYear());
    if (type === 1) return MON[d.getUTCMonth()];
    if (type === 2) return String(d.getUTCDate());
    return `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
  }

  crossTime(t) {
    const d = new Date(t * 1000), p = (n) => String(n).padStart(2, "0");
    return this.blind ? `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}` : fmtTime(t);
  }

  // ── coordinates ──
  paneRect() {
    const panes = this.chart.panes();
    const h = panes.length ? panes[0].getHeight() : this.el.clientHeight;
    const w = this.el.clientWidth - this.chart.priceScale("right").width();
    return { x: 0, y: 0, w: Math.max(0, w), h };
  }
  timeToX(t) { const l = timeToLogical(this.times, t, this.tfSec); return this.chart.timeScale().logicalToCoordinate(l); }
  priceToY(p) { return this.candles.priceToCoordinate(p); }
  xToTime(x) { const l = this.chart.timeScale().coordinateToLogical(x); return l == null ? null : logicalToTime(this.times, l, this.tfSec); }
  yToPrice(y) { return this.candles.coordinateToPrice(y); }
  project(pt) { const x = this.timeToX(pt.time), y = this.priceToY(pt.price); return x == null || y == null ? null : { x, y }; }
  barAt(t) { const i = this.times.indexOf(t); return i >= 0 ? this.bars[i] : null; }

  /** magnet: snap a price to the nearest O/H/L/C of the bar at `time` when within 12 px */
  snap(time, price) {
    const b = this.shownAt(time);
    if (!b) return price;
    let best = price, bestD = 12;
    for (const v of [b.open, b.high, b.low, b.close]) {
      const d = Math.abs(this.priceToY(v) - this.priceToY(price));
      if (d < bestD) { bestD = d; best = v; }
    }
    return best;
  }

  // ── overlay ──
  requestDraw() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => { this._raf = null; this.draw(); });
  }

  draw() {
    const dpr = window.devicePixelRatio || 1;
    const W = this.el.clientWidth, H = this.el.clientHeight;
    if (this.canvas.width !== W * dpr || this.canvas.height !== H * dpr) {
      this.canvas.width = W * dpr; this.canvas.height = H * dpr;
      this.canvas.style.width = W + "px"; this.canvas.style.height = H + "px";
    }
    const ctx = this.ctx;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const r = this.paneRect();
    ctx.save();
    ctx.beginPath(); ctx.rect(r.x, r.y, r.w, r.h); ctx.clip();
    for (const layer of this.layers) { try { layer(ctx, this, r); } catch (e) { console.warn("[chartlab] layer", e); } }
    ctx.restore();
    this.updateCountdown();
  }

  /** Live: seconds until the forming candle closes (IST clock). Replay with 1-minute steps: the same at the replay
   *  clock. Hidden when the last candle is already closed (market shut, bar-step replay). */
  updateCountdown() {
    const el = this.countdown, last = this.bars[this.bars.length - 1];
    const now = this.knownUntil != null ? this.knownUntil : Date.now() / 1000 + 19800;   // IST wall time as UTC
    const close = last ? candleCloseAt(last.time, this.tf, this.session.start, this.session.end) : 0;
    const y = last ? this.priceToY(last.close) : null;
    const r = this.paneRect();
    if (!last || now < last.time || now >= close || y == null || y < 0 || y > r.h - 20) { el.style.display = "none"; return; }
    el.textContent = fmtCountdown(close - now);
    el.style.display = "block";
    el.style.top = `${Math.round(y + 10)}px`;
    el.style.left = `${r.w + 1}px`;
    el.style.width = `${Math.max(0, this.el.clientWidth - r.w - 1)}px`;
    el.style.background = last.close >= last.open ? COLORS.up : COLORS.down;
  }

  /** Draw server-side indicator shapes (segments, boxes, polygons, polylines, labels). */
  drawShapes(ctx, shapes, replay) {
    const ku = this.knownUntil;
    const lastT = this.bars.length ? this.bars[this.bars.length - 1].time : null;
    for (const s of visibleAt(shapes, ku, replay, this.tfSec)) {
      if (s.kind === "footprint") { this.drawFootprint(ctx, s); continue; }
      const pts0 = s.clip && ku != null ? clipPoints(s.points, lastT) : s.points;
      const pts = pts0.map((p) => this.project(p));
      if (pts.some((p) => !p)) continue;
      ctx.strokeStyle = s.color || "#888";
      ctx.lineWidth = s.width || 1;
      ctx.setLineDash(s.dash === 1 ? [2, 3] : s.dash === 2 ? [6, 4] : []);
      if (s.kind === "segment" || s.kind === "polyline") {
        ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y))); ctx.stroke();
        if (s.text && s.kind === "segment") label(ctx, s.text, pts[pts.length - 1].x + 4, pts[0].y, s.color, "left", 10);
      } else if (s.kind === "box") {
        const x = Math.min(pts[0].x, pts[1].x), y = Math.min(pts[0].y, pts[1].y);
        const w = Math.abs(pts[1].x - pts[0].x), h = Math.abs(pts[1].y - pts[0].y);
        if (s.fill) { ctx.fillStyle = s.fill; ctx.fillRect(x, y, w, h); }
        ctx.strokeRect(x, y, w, h);
        if (s.text) label(ctx, s.text, x + 2, y - 3, s.color, "left", 10);
      } else if (s.kind === "polygon") {
        ctx.beginPath(); pts.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y))); ctx.closePath();
        if (s.fill) { ctx.fillStyle = s.fill; ctx.fill(); }
      } else if (s.kind === "label") {
        if (s.small) label(ctx, s.text, pts[0].x, pts[0].y, s.color, "center", 10);
        else badge(ctx, s.text, pts[0].x, pts[0].y, s.color);
      }
    }
    ctx.setLineDash([]);
  }

  /** One footprint candle: sell × buy at each price level (numbers when zoomed in, colour heat otherwise),
   *  diagonal imbalances, the candle's POC box and its delta. */
  drawFootprint(ctx, s) {
    if (this.times.indexOf(s.time) < 0) return;                      // only on a candle that is on this chart
    const x = this.timeToX(s.time);
    if (x == null || !s.rows.length) return;
    const bs = this.chart.timeScale().options().barSpacing || 8, half = bs * 0.48;
    let L = s.level, rows = s.rows;
    const y0 = this.priceToY(rows[0][0]), y1 = this.priceToY(rows[0][0] + L);
    if (y0 == null || y1 == null) return;
    const pxPer = Math.abs(y0 - y1);
    if (s.mode !== "heat" && bs >= 56 && pxPer < 13) {          // merge price rows until the numbers fit
      const k = Math.ceil(13 / Math.max(pxPer, 0.5)), big = L * k, m = new Map();
      for (const [p, sl, by] of rows) {
        const q = Math.floor(p / big + 1e-9) * big, r = m.get(q) || [q, 0, 0];
        r[1] += sl; r[2] += by; m.set(q, r);
      }
      rows = [...m.values()].sort((a, b) => b[0] - a[0]);
      L = big;
      s = { ...s, poc: rows.reduce((a, r) => (r[1] + r[2] > a[1] + a[2] ? r : a))[0] };
    }
    const yTop = this.priceToY(rows[0][0] + L), yBot = this.priceToY(rows[rows.length - 1][0]);
    if (yTop == null || yBot == null) return;
    const h = (yBot - yTop) / rows.length;
    const numbers = s.mode === "numbers" || (s.mode !== "heat" && bs >= 64 && h >= 10);
    const maxv = Math.max(1e-9, ...rows.map((r) => Math.max(r[1], r[2])));
    const fmt = (v) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v >= 100 ? Math.round(v).toString()
      : v >= 10 ? v.toFixed(1) : v > 0 ? v.toFixed(2) : "0");
    rows.forEach(([p, sell, buy], i) => {
      const y = this.priceToY(p + L);
      if (y == null) return;
      if (numbers) { ctx.fillStyle = "rgba(19,23,34,0.88)"; ctx.fillRect(x - half, y, half * 2, h); }
      ctx.fillStyle = `rgba(239,83,80,${(0.75 * sell / maxv).toFixed(3)})`;
      ctx.fillRect(x - half, y, half, h);
      ctx.fillStyle = `rgba(38,166,154,${(0.75 * buy / maxv).toFixed(3)})`;
      ctx.fillRect(x, y, half, h);
      if (Math.abs(p - s.poc) < L / 2) { ctx.strokeStyle = "#ffeb3b"; ctx.lineWidth = 1; ctx.setLineDash([]); ctx.strokeRect(x - half, y, half * 2, h); }
      if (!numbers) return;
      const below = rows[i + 1], above = rows[i - 1];                // diagonal: buy vs the sell one level lower
      const buyImb = below && buy > 0 && buy >= s.imb * Math.max(below[1], 1e-9);
      const sellImb = above && sell > 0 && sell >= s.imb * Math.max(above[2], 1e-9);
      const fs = Math.max(8, Math.min(11, Math.floor(h - 1)));
      ctx.font = `${sellImb ? "bold " : ""}${fs}px Inter, system-ui, sans-serif`;
      ctx.textBaseline = "middle"; ctx.textAlign = "right";
      ctx.fillStyle = sellImb ? "#ff5252" : "#e0a3a3";
      ctx.fillText(fmt(sell), x - 3, y + h / 2);
      ctx.font = `${buyImb ? "bold " : ""}${fs}px Inter, system-ui, sans-serif`;
      ctx.textAlign = "left";
      ctx.fillStyle = buyImb ? "#00e676" : "#9fd9d2";
      ctx.fillText(fmt(buy), x + 3, y + h / 2);
    });
    if (bs >= 22) {
      const d = s.delta;
      label(ctx, `Δ${d >= 0 ? "+" : ""}${fmt(Math.abs(d)).replace(/^/, d < 0 ? "-" : "")}`, x, yBot + 9, d >= 0 ? "#26a69a" : "#ef5350", "center", 10);
    }
  }

  /** review lines (trade review): kept apart from the practice panel's lines, which it redraws on every bar */
  setReviewLines(lines) {
    for (const l of this.reviewLines || []) this.candles.removePriceLine(l);
    this.reviewLines = (lines || []).filter((x) => x.price != null && isFinite(x.price)).map((x) => this.candles.createPriceLine({
      price: x.price, color: x.color, lineWidth: 1, lineStyle: x.dash ? LC.LineStyle.Dashed : LC.LineStyle.Solid,
      axisLabelVisible: true, title: x.title }));
  }

  clearPriceLines() { for (const l of this.priceLines) this.candles.removePriceLine(l); this.priceLines = []; }
  addPriceLine(price, color, title) {
    this.priceLines.push(this.candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title }));
  }
}

export function label(ctx, text, x, y, color, align = "left", size = 11) {
  ctx.font = `${size}px Inter, system-ui, sans-serif`;
  ctx.textAlign = align; ctx.textBaseline = "middle";
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
}

export function badge(ctx, text, x, y, color) {
  ctx.font = "bold 11px Inter, system-ui, sans-serif";
  const w = ctx.measureText(text).width + 8;
  ctx.fillStyle = color;
  ctx.beginPath(); ctx.roundRect ? ctx.roundRect(x - w / 2, y - 8, w, 16, 3) : ctx.rect(x - w / 2, y - 8, w, 16); ctx.fill();
  ctx.fillStyle = "#fff"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(text, x, y);
}
