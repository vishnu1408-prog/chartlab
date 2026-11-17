// Drawing tools on the overlay canvas. Anchors are stored as {time, price} so drawings survive zoom, scroll,
// timeframe changes and reloads (saved per symbol on the server).
import { TOOL_POINTS, PATTERNS, rangeProfile, fibPrices, fibExtPrices, FIB_EXT_LEVELS, FIB_RET, FIB_EXT, FIB_COLORS, rrLevels, hitTest, fmtPrice, fmtTime, isHigh, patternRatios, intersect, timeToLogical } from "./core.js";
import { label, badge } from "./chart.js";

const DEFAULT_COLOR = { trend: "#2962ff", ray: "#2962ff", hline: "#f5a623", vline: "#787b86", rect: "#7e57c2", fib: "#90a4ae", fibext: "#90a4ae",
                        text: "#d1d4dc", pen: "#ffeb3b", measure: "#2962ff", rr: "#26a69a", frvp: "#2962ff", cyclic: "#7e57c2", timecycles: "#7e57c2", sine: "#7e57c2" };

export class Drawings {
  constructor(view, { onChange, onSelect, onTool }) {
    this.view = view;
    this.items = [];
    this.tool = "cursor";
    this.draft = null;            // drawing being created
    this.selected = null;
    this.drag = null;
    this.magnet = true;
    this.stayInTool = false;
    this.lockAll = false;            // 🔒 all drawings: dragging over them always moves the chart
    this._save = onChange || (() => {});
    this.onChange = (items) => { this.commit(); this._save(items); };   // every change is an undo step
    this.history = []; this.future = []; this._last = "[]";
    this.onSelect = onSelect || (() => {});
    this.onTool = onTool || (() => {});
    view.layers.push((ctx, v, r) => this.render(ctx, r));
    const el = view.el;
    el.addEventListener("mousedown", (e) => this.down(e), true);
    el.addEventListener("mousemove", (e) => this.move(e), true);
    window.addEventListener("mouseup", (e) => this.up(e), true);
    el.addEventListener("dblclick", (e) => { if (this.selected) { this.onSelect(this.selected, true); e.stopPropagation(); } }, true);
  }

  setItems(items) {
    this.items = items || []; this.selected = null;
    this.history = []; this.future = []; this._last = JSON.stringify(this.items);   // new symbol: fresh history
    this.view.requestDraw();
  }

  /** remember the drawings as they are now (call after any change); keeps the last 100 steps */
  commit() {
    const cur = JSON.stringify(this.items);
    if (cur === this._last) return;
    this.history.push(this._last);
    if (this.history.length > 100) this.history.shift();
    this.future = [];
    this._last = cur;
  }

  _restore(json) {
    this._last = json;
    this.items = JSON.parse(json);
    this.selected = null; this.draft = null; this.drag = null;
    this._save(this.items);
    this.view.requestDraw();
  }

  undo() {
    if (this.draft) { this.cancel(); return true; }
    this.commit();
    if (!this.history.length) return false;
    this.future.push(this._last);
    this._restore(this.history.pop());
    return true;
  }

  redo() {
    if (!this.future.length) return false;
    this.history.push(this._last);
    this._restore(this.future.pop());
    return true;
  }

  setTool(t) {
    this.tool = t;
    this.draft = null;
    const drawing = t !== "cursor";
    this.view.chart.applyOptions({ handleScroll: !drawing, handleScale: !drawing });
    this.view.el.classList.toggle("drawing", drawing);
    this.onTool(t);
    this.view.requestDraw();
  }

  local(e) {
    const r = this.view.el.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  anchorAt(p) {
    const time = this.view.xToTime(p.x);
    let price = this.view.yToPrice(p.y);
    if (time == null || price == null) return null;
    if (this.magnet) price = this.view.snap(time, price);
    return { time, price };
  }

  inPane(p) { const r = this.view.paneRect(); return p.x >= 0 && p.x <= r.w && p.y >= 0 && p.y <= r.h; }

  down(e) {
    if (e.button !== 0) return;
    const p = this.local(e);
    if (!this.inPane(p)) return;
    if (this.lastMeasure && !this.draft) { this.lastMeasure = null; this.view.requestDraw(); }
    if (this.tool === "pen") {
      const time = this.view.xToTime(p.x), price = this.view.yToPrice(p.y);
      if (time == null || price == null) return;
      e.stopPropagation(); e.preventDefault();
      this.draft = { id: crypto.randomUUID ? crypto.randomUUID() : String(Date.now() + Math.random()), type: "pen",
                     points: [{ time, price }], color: this.penColor || DEFAULT_COLOR.pen, width: this.penWidth || 2 };
      this.penLast = p;
      this.view.requestDraw();
      return;
    }
    if (this.tool !== "cursor") {
      const a = this.anchorAt(p);
      if (!a) return;
      e.stopPropagation(); e.preventDefault();
      if (!this.draft) {
        this.draft = { id: crypto.randomUUID ? crypto.randomUUID() : String(Date.now() + Math.random()), type: this.tool,
                       points: [a, { ...a }], color: DEFAULT_COLOR[this.tool], width: 2, text: this.tool === "text" ? "Text" : "", ratio: 2,
                       ...(this.tool === "frvp" ? { rows: 24 } : {}) };
        if (TOOL_POINTS[this.tool] === 1) this.finish();
      } else {
        this.draft.points[this.draft.points.length - 1] = a;
        if (this.draft.points.length >= TOOL_POINTS[this.draft.type]) this.finish();
        else this.draft.points.push({ ...a });
      }
      this.view.requestDraw();
      return;
    }
    const hit = this.hit(p);
    if (hit && (hit.item.locked || this.lockAll)) {
      // locked: let the chart take the drag; a plain click (no movement) still selects it, so it can be unlocked
      this.lockedClick = { item: hit.item, x: p.x, y: p.y };
      return;
    }
    if (hit) {
      e.stopPropagation(); e.preventDefault();
      this.selected = hit.item;
      this.onSelect(hit.item, false);
      this.drag = { item: hit.item, part: hit.part, index: hit.index, start: this.anchorAt(p), orig: hit.item.points.map((q) => ({ ...q })) };
      this.view.chart.applyOptions({ handleScroll: false, handleScale: false });
    } else if (this.selected) {
      this.selected = null;
      this.onSelect(null, false);
    }
    this.view.requestDraw();
  }

  move(e) {
    const p = this.local(e);
    if (this.draft?.type === "pen") {                          // freehand: a point every 2 px while the button is held
      if (!(e.buttons & 1)) return;
      if (Math.hypot(p.x - this.penLast.x, p.y - this.penLast.y) < 2) return;
      const time = this.view.xToTime(p.x), price = this.view.yToPrice(p.y);
      if (time == null || price == null) return;
      this.draft.points.push({ time, price });
      this.penLast = p;
      this.view.requestDraw();
      return;
    }
    if (this.draft) {
      const a = this.anchorAt(p);
      if (a) { this.draft.points[this.draft.points.length - 1] = a; this.view.requestDraw(); }
      return;
    }
    if (this.drag) {
      const a = this.anchorAt(p);
      if (!a || !this.drag.start) return;
      const d = this.drag;
      if (d.part === "handle") d.item.points[d.index] = a;
      else {
        const dt = a.time - d.start.time, dp = a.price - d.start.price;
        d.item.points = d.orig.map((q) => ({ time: q.time + dt, price: q.price + dp }));
      }
      this.view.requestDraw();
      return;
    }
    if (this.tool === "cursor") { const h = this.hit(p); this.view.el.classList.toggle("hovering", !!h && !h.item.locked && !this.lockAll); }
  }

  up(e) {
    if (this.lockedClick) {
      const lc = this.lockedClick, p = e ? this.local(e) : null;
      this.lockedClick = null;
      if (p && Math.hypot(p.x - lc.x, p.y - lc.y) < 4) { this.selected = lc.item; this.onSelect(lc.item, false); this.view.requestDraw(); }
    }
    if (this.draft?.type === "pen") {
      const d = this.draft;
      this.draft = null;
      if (d.points.length >= 2) {
        this.items.push(d); this.onChange(this.items);
        this.penColor = d.color; this.penWidth = d.width;
      }
      this.view.requestDraw();                               // the pen stays on until Esc / another tool
      return;
    }
    if (this.drag) {
      this.fitProfile(this.drag.item);
      this.drag = null;
      this.view.chart.applyOptions({ handleScroll: true, handleScale: true });
      this.onChange(this.items);
    }
  }

  /** a volume profile's handles sit on its high / low, so the box hugs the bars it covers */
  fitProfile(it) {
    if (it.type !== "frvp") return;
    const P = rangeProfile(this.view.bars, it.points[0].time, it.points[1].time, it.rows || 24);
    if (!P) return;
    const [p, q] = it.points;
    p.price = p.time <= q.time ? P.hi : P.lo; q.price = p.time <= q.time ? P.lo : P.hi;
  }

  finish() {
    const d = this.draft;
    this.draft = null;
    this.fitProfile(d);
    if (d.type === "measure") this.lastMeasure = d;        // shown until the next click, never saved
    else { this.items.push(d); this.selected = d; this.onChange(this.items); this.onSelect(d, d.type === "text"); }
    if (!this.stayInTool) this.setTool("cursor");
  }

  cancel() { this.draft = null; this.setTool("cursor"); }

  remove(item = this.selected) {
    if (!item) return;
    this.items = this.items.filter((x) => x !== item);
    this.selected = null;
    this.onSelect(null, false);
    this.onChange(this.items);
    this.view.requestDraw();
  }

  clear() { this.items = []; this.selected = null; this.onChange(this.items); this.view.requestDraw(); }

  hit(p) {
    const w = this.view.paneRect().w;
    for (let i = this.items.length - 1; i >= 0; i--) {
      const it = this.items[i];
      const pts = it.points.map((q) => this.view.project(q));
      if (pts.some((q) => !q)) continue;
      const h = hitTest(it.type, pts, p.x, p.y, w);
      if (h) return { item: it, ...h };
    }
    return null;
  }

  render(ctx, r) {
    for (const it of this.items) this.drawItem(ctx, it, r, it === this.selected);
    if (this.draft) this.drawItem(ctx, this.draft, r, true);
    if (this.lastMeasure) this.drawItem(ctx, this.lastMeasure, r, false);
  }

  drawItem(ctx, it, r, selected) {
    const v = this.view;
    const pts = it.points.map((q) => v.project(q));
    if (pts.some((q) => !q)) return;
    const c = it.color || "#2962ff";
    ctx.strokeStyle = c; ctx.fillStyle = c; ctx.lineWidth = it.width || 2; ctx.setLineDash(it.dash ? [6, 4] : []);
    const [a, b] = pts;
    const seg = (p, q) => { ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke(); };
    switch (it.type) {
      case "trend": seg(a, b); break;
      case "ray": {
        const k = b.x !== a.x ? (b.y - a.y) / (b.x - a.x) : 0;
        const ex = b.x >= a.x ? r.w : 0;
        seg(a, { x: ex, y: a.y + k * (ex - a.x) }); break;
      }
      case "hline": seg({ x: 0, y: a.y }, { x: r.w, y: a.y }); priceTag(ctx, it.points[0].price, r.w, a.y, c); break;
      case "vline": seg({ x: a.x, y: 0 }, { x: a.x, y: r.h }); break;
      case "rect": {
        ctx.globalAlpha = 0.15; ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y));
        ctx.globalAlpha = 1; ctx.strokeRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y)); break;
      }
      case "frvp": this.drawProfile(ctx, it, a, b, r, c); break;
      case "fib": {
        // levels per drawing (default 0, 0.5 … 4: 0 at the end of the drag, 1 at its start, then beyond the start)
        const p0 = it.points[0].price, p1 = it.points[1].price;
        const lv = fibPrices(it.reverse ? p1 : p0, it.reverse ? p0 : p1, it.levels || FIB_EXT_LEVELS);
        const pal = ["#787b86", "#f23645", "#ff9800", "#4caf50", "#089981", "#00bcd4", "#2962ff", "#9c27b0", "#e91e63"];
        const colOf = (k) => pal[k % pal.length];
        const x1 = Math.min(a.x, b.x), x2 = it.extend === false ? Math.max(a.x, b.x) : r.w;
        const ys = lv.map(({ price }) => v.priceToY(price));
        if (it.fill !== false) {
          for (let k = 0; k + 1 < lv.length; k++) {
            if (ys[k] == null || ys[k + 1] == null) continue;
            ctx.globalAlpha = 0.08;
            ctx.fillStyle = colOf(k + 1);
            ctx.fillRect(x1, Math.min(ys[k], ys[k + 1]), x2 - x1, Math.abs(ys[k + 1] - ys[k]));
          }
          ctx.globalAlpha = 1;
        }
        lv.forEach(({ level, price }, k) => {
          const y = ys[k];
          if (y == null) return;
          const col = colOf(k);
          ctx.strokeStyle = col;
          ctx.lineWidth = 1;
          seg({ x: x1, y }, { x: x2, y });
          label(ctx, `${level} (${fmtPrice(price)})`, x1 + 4, y - 7, col, "left", 10);
        });
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]); ctx.strokeStyle = c; seg(a, b); ctx.setLineDash([]); break;
      }
      case "fibext": {
        // trend-based Fib extension: 0 at the end of the swing, levels beyond it in the swing direction
        const lv = fibExtPrices(it.points[0].price, it.points[1].price, it.levels || FIB_EXT_LEVELS);
        const pal = ["#787b86", "#f23645", "#ff9800", "#4caf50", "#089981", "#00bcd4", "#2962ff", "#9c27b0", "#e91e63"];
        const x1 = b.x, x2 = it.extend === false ? b.x + 220 : r.w;
        const ys = lv.map(({ price }) => v.priceToY(price));
        if (it.fill !== false) {
          for (let k = 0; k + 1 < lv.length; k++) {
            if (ys[k] == null || ys[k + 1] == null) continue;
            ctx.globalAlpha = 0.07; ctx.fillStyle = pal[(k + 1) % pal.length];
            ctx.fillRect(x1, Math.min(ys[k], ys[k + 1]), x2 - x1, Math.abs(ys[k + 1] - ys[k]));
          }
          ctx.globalAlpha = 1;
        }
        lv.forEach(({ level, price }, k) => {
          const y = ys[k];
          if (y == null) return;
          const col = pal[k % pal.length];
          ctx.strokeStyle = col; ctx.lineWidth = 1;
          seg({ x: x1, y }, { x: x2, y });
          label(ctx, `${level} (${fmtPrice(price)})`, x1 + 4, y - 7, col, "left", 10);
        });
        ctx.setLineDash([3, 3]); ctx.strokeStyle = c; seg(a, b); ctx.setLineDash([]); break;
      }
      case "text": label(ctx, it.text || "Text", a.x, a.y, c, "left", 13); break;
      case "pen": {
        ctx.lineJoin = "round"; ctx.lineCap = "round";
        ctx.beginPath(); pts.forEach((q, i) => (i ? ctx.lineTo(q.x, q.y) : ctx.moveTo(q.x, q.y))); ctx.stroke();
        ctx.lineJoin = "miter"; ctx.lineCap = "butt";
        break;
      }
      case "measure": {
        const p0 = it.points[0], p1 = it.points[1];
        const dp = p1.price - p0.price, pct = (dp / p0.price) * 100;
        const nb = Math.round((p1.time - p0.time) / v.tfSec);
        ctx.globalAlpha = 0.18; ctx.fillStyle = dp >= 0 ? "#2962ff" : "#ef5350";
        ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y)); ctx.globalAlpha = 1;
        badge(ctx, `${dp >= 0 ? "+" : ""}${fmtPrice(dp)} (${pct.toFixed(2)}%)  ${nb} bars`, (a.x + b.x) / 2, Math.min(a.y, b.y) - 12, dp >= 0 ? "#2962ff" : "#ef5350");
        break;
      }
      case "rr": {
        const L = rrLevels(it.points[0].price, it.points[1].price, it.ratio || 2);
        const x1 = Math.min(a.x, b.x), x2 = Math.max(b.x, a.x + 60);
        const ye = v.priceToY(L.entry), ys = v.priceToY(L.stop), yt = v.priceToY(L.target);
        ctx.globalAlpha = 0.2;
        ctx.fillStyle = "#ef5350"; ctx.fillRect(x1, Math.min(ye, ys), x2 - x1, Math.abs(ys - ye));
        ctx.fillStyle = "#26a69a"; ctx.fillRect(x1, Math.min(ye, yt), x2 - x1, Math.abs(yt - ye));
        ctx.globalAlpha = 1; ctx.strokeStyle = "#d1d4dc"; ctx.lineWidth = 1; seg({ x: x1, y: ye }, { x: x2, y: ye });
        label(ctx, `Target ${fmtPrice(L.target)}  +${fmtPrice(L.reward)} pts`, x1 + 4, yt + (yt < ye ? 9 : -9), "#26a69a", "left", 10);
        label(ctx, `Stop ${fmtPrice(L.stop)}  −${fmtPrice(L.risk)} pts`, x1 + 4, ys + (ys > ye ? -9 : 9), "#ef5350", "left", 10);
        label(ctx, `${L.side > 0 ? "LONG" : "SHORT"}  R:R 1:${it.ratio || 2}`, x1 + 4, ye - 8, "#d1d4dc", "left", 10);
        break;
      }
      default:
        if (PATTERNS[it.type]?.cycle) this.drawCycle(ctx, it, pts, r, c);
        else if (PATTERNS[it.type]) { this.drawPattern(ctx, it, pts, r, c); if (selected && this.zoneHook) this.zoneHook(ctx, it, pts); }
    }
    if (selected && (it.locked || this.lockAll)) label(ctx, "🔒", pts[pts.length - 1].x + 8, pts[pts.length - 1].y - 8, c, "left", 12);
    else if (selected) {
      ctx.fillStyle = "#fff"; ctx.strokeStyle = c; ctx.lineWidth = 2;
      for (const p of pts) { ctx.beginPath(); ctx.arc(p.x, p.y, 4.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); }
    }
    ctx.setLineDash([]);
  }
}

/** XABCD, Cypher, Head & shoulders, ABCD, Triangle, Three drives and Elliott waves: zigzag + fills + ratios + labels */
Drawings.prototype.drawPattern = function (ctx, it, pts, r, c) {
  const spec = PATTERNS[it.type], n = pts.length, pr = it.points.map((q) => q.price);
  const poly = (list, fill) => {
    ctx.beginPath(); list.forEach((p, k) => (k ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    if (fill) { ctx.closePath(); ctx.fill(); } else ctx.stroke();
  };
  ctx.fillStyle = c;
  ctx.globalAlpha = 0.14;
  for (const tri of spec.fill || []) if (tri.every((i) => i < n)) poly(tri.map((i) => pts[i]), true);
  ctx.globalAlpha = 1;
  if (it.type === "triangle" && n >= 4) {                    // two converging lines A→C and B→D, extended to the apex
    const apex = intersect(pts[0], pts[2], pts[1], pts[3]);
    const span = Math.max(1, pts[3].x - pts[0].x);
    const ok = apex && apex.x > Math.max(pts[2].x, pts[3].x) && apex.x < pts[3].x + 3 * span;
    const ext = (p, q, x) => ({ x, y: p.y + ((q.y - p.y) * (x - p.x)) / ((q.x - p.x) || 1) });
    const xEnd = pts[3].x + span * 0.3;
    const e1 = ok ? apex : ext(pts[0], pts[2], xEnd), e2 = ok ? apex : ext(pts[1], pts[3], xEnd);
    ctx.globalAlpha = 0.12; poly([pts[0], e1, e2, pts[1]], true); ctx.globalAlpha = 1;
    poly([pts[0], e1]); poly([pts[1], e2]);
  }
  if (it.type === "hs" && n >= 5) {                          // neckline through the two troughs, across the whole pattern
    const p = pts[2], q = pts[4], x0 = pts[0].x, x1 = (pts[6] || pts[5] || q).x;
    const y = (x) => p.y + ((q.y - p.y) * (x - p.x)) / ((q.x - p.x) || 1);
    ctx.save(); ctx.setLineDash([6, 4]); ctx.lineWidth = 1.5; poly([{ x: x0, y: y(x0) }, { x: x1, y: y(x1) }]); ctx.restore();
    label(ctx, "Neckline", x1 + 4, y(x1), c, "left", 10);
  }
  if (spec.wave) { ctx.save(); ctx.lineWidth = Math.max(1, (it.width || 2) - 0.5); poly(pts); ctx.restore(); }
  else poly(pts);
  const ratios = patternRatios(it.type, pr);
  ctx.save(); ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
  for (const { i, j, value } of ratios) {
    if (j >= n || !isFinite(value)) continue;
    poly([pts[i], pts[j]]);
    badge(ctx, value.toFixed(3), (pts[i].x + pts[j].x) / 2, (pts[i].y + pts[j].y) / 2, "#363a45");
  }
  ctx.restore();
  pts.forEach((p, i) => {
    const t = spec.labels[i];
    if (t == null) return;
    const up = isHigh(pr, i);
    badge(ctx, spec.wave ? `(${t})` : t, p.x, p.y + (up ? -14 : 14), c);
  });
};

/** Fixed range volume profile: histogram from the left edge of the range, value area bright, POC + VAH / VAL lines.
 *  Uses only the bars on the chart — in replay that is only what has been revealed, so it never shows the future. */
Drawings.prototype.drawProfile = function (ctx, it, a, b, r, c) {
  const v = this.view;
  const P = rangeProfile(v.bars, it.points[0].time, it.points[1].time, it.rows || 24);
  const x1 = Math.min(a.x, b.x), x2 = Math.max(a.x, b.x);
  if (!P) { ctx.globalAlpha = 0.5; ctx.strokeRect(x1, Math.min(a.y, b.y), x2 - x1, Math.abs(b.y - a.y)); ctx.globalAlpha = 1; return; }
  const yHi = v.priceToY(P.hi), yLo = v.priceToY(P.lo);
  if (yHi == null || yLo == null) return;
  const maxW = Math.max(40, (x2 - x1) * 0.4);
  const top = Math.max(...P.rows.map((q) => q.vol)) || 1;
  ctx.save();
  ctx.globalAlpha = 0.06; ctx.fillStyle = c; ctx.fillRect(x1, yHi, x2 - x1, yLo - yHi);
  ctx.globalAlpha = 0.35; ctx.strokeStyle = c; ctx.lineWidth = 1; ctx.strokeRect(x1, yHi, x2 - x1, yLo - yHi);
  P.rows.forEach((q, i) => {
    const y0 = v.priceToY(q.hi), y1 = v.priceToY(q.lo);
    if (y0 == null || y1 == null) return;
    const h = Math.max(1, y1 - y0 - 1), inVA = i >= P.vaLo && i <= P.vaHi;
    const wu = (q.up / top) * maxW, wd = (q.down / top) * maxW;
    ctx.globalAlpha = inVA ? 0.6 : 0.25;
    ctx.fillStyle = "#26a69a"; ctx.fillRect(x1, y0, wu, h);
    ctx.fillStyle = "#ef5350"; ctx.fillRect(x1 + wu, y0, wd, h);
  });
  ctx.globalAlpha = 1;
  const line = (price, color, dash, text) => {
    const y = v.priceToY(price); if (y == null) return;
    ctx.strokeStyle = color; ctx.setLineDash(dash); ctx.lineWidth = dash.length ? 1 : 2;
    ctx.beginPath(); ctx.moveTo(x1, y); ctx.lineTo(x2, y); ctx.stroke(); ctx.setLineDash([]);
    label(ctx, `${text} ${fmtPrice(price)}`, x2 + 4, y, color, "left", 10);
  };
  line(P.vah, "#90a4ae", [4, 3], "VAH"); line(P.val, "#90a4ae", [4, 3], "VAL"); line(P.pocPrice, "#f5a623", [], "POC");
  const what = P.mode === "volume" ? "Volume" : "Time at price (no volume for indices)";
  label(ctx, `${what} · ${P.bars} bars`, x1 + 2, yHi - 9, "#787b86", "left", 10);
  ctx.restore();
};

/** Cyclic lines / time cycles / sine line, spaced by the bars between the two clicks (gaps between sessions = 1 bar) */
Drawings.prototype.drawCycle = function (ctx, it, pts, r, c) {
  const v = this.view, ts = v.chart.timeScale();
  const [a, b] = pts;
  const la = timeToLogical(v.times, it.points[0].time, v.tfSec), lb = timeToLogical(v.times, it.points[1].time, v.tfSec);
  const step = Math.abs(lb - la);
  ctx.save(); ctx.setLineDash([4, 4]); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); ctx.restore();
  if (step < 0.5) return;
  const X = (l) => ts.logicalToCoordinate(l);
  const vis = ts.getVisibleLogicalRange();
  const s0 = Math.min(la, lb), end = (vis ? vis.to : s0 + step * 40) + step;
  if (it.type === "cyclic") {
    for (let l = s0, k = 0; l <= end && k < 400; l += step, k++) {
      const x = X(l); if (x == null) continue;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, r.h); ctx.stroke();
    }
  } else if (it.type === "timecycles") {
    const ry = Math.max(8, Math.abs(b.y - a.y)), up = b.y <= a.y;
    for (let l = s0, k = 0; l + step <= end && k < 400; l += step, k++) {
      const x1 = X(l), x2 = X(l + step); if (x1 == null || x2 == null) continue;
      ctx.beginPath(); ctx.ellipse((x1 + x2) / 2, a.y, Math.abs(x2 - x1) / 2, ry, 0, up ? Math.PI : 0, up ? 2 * Math.PI : Math.PI); ctx.stroke();
    }
  } else {                                                   // sine: first click = peak/trough, second = the next trough/peak
    const mid = (a.y + b.y) / 2, amp = (a.y - b.y) / 2;
    ctx.beginPath();
    let first = true;
    for (let l = la; l <= end; l += Math.max(step / 24, 0.25)) {
      const x = X(l); if (x == null) continue;
      const y = mid + amp * Math.cos((Math.PI * (l - la)) / (lb - la));
      first ? ctx.moveTo(x, y) : ctx.lineTo(x, y); first = false;
    }
    ctx.stroke();
  }
};

function priceTag(ctx, price, x, y, color) {
  ctx.font = "11px Inter, system-ui, sans-serif";
  const t = fmtPrice(price), w = ctx.measureText(t).width + 8;
  ctx.fillStyle = color; ctx.fillRect(x - w, y - 8, w, 16);
  ctx.fillStyle = "#000"; ctx.textAlign = "right"; ctx.textBaseline = "middle"; ctx.fillText(t, x - 4, y);
}

export const TOOLS = [
  { id: "cursor", icon: "➚", title: "Cursor (Esc)" },
  { id: "trend", icon: "╱", title: "Trend line (Alt+T)" },
  { id: "ray", icon: "↗", title: "Ray" },
  { id: "hline", icon: "―", title: "Horizontal line (Alt+H)" },
  { id: "vline", icon: "│", title: "Vertical line (Alt+V)" },
  { id: "rect", icon: "▭", title: "Rectangle (Alt+R)" },
  { id: "fib", icon: "𝐅", title: "Fibonacci retracement (Alt+F)" },
  { id: "fibext", icon: "𝐅↗", title: "Trend-based Fib extension (Alt+E): click the start and the end of the swing — levels 0, 0.5 … 4 continue beyond its end (edit levels in the drawing's settings)" },
  { id: "patterns", icon: "⋀⋁", title: "Patterns: XABCD, Cypher, Head & shoulders, ABCD, Triangle, Three drives, Elliott waves, Cycles", menu: true },
  { id: "frvp", icon: "▤", title: "Fixed range volume profile: click the start and the end of the range" },
  { id: "rr", icon: "⇕", title: "Long/short position: click entry, then stop (target = R × risk)" },
  { id: "measure", icon: "⟷", title: "Measure (Shift+drag style: 2 clicks)" },
  { id: "text", icon: "T", title: "Text note" },
  { id: "pen", icon: "✎", title: "Pen: hold the mouse button and draw freehand (Alt+P) · Esc to stop" },
];
export { fmtTime };
