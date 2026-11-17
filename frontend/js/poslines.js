// Open paper positions on the charts: entry, stop and target as lines on the option's own premium chart (levels in
// option ₹) and on the underlying's chart (levels on the index). Stop / target lines can be dragged; the new level is
// saved on the position (the server watches it every 2 s, like the ticket's levels).
import { optionChartName, fmtPrice } from "./core.js";

const LC = window.LightweightCharts;
const COL = { entry: "#5b9cf6", stop: "#ef5350", target: "#26a69a" };

export class PositionLines {
  constructor(app) {
    this.app = app;
    this.snap = null;
    this.drawn = new WeakMap();     // panel → { key: string, lines: [{ kind, pos, line, price }] }
    this.dragging = null;
  }

  /** the paper snapshot changed (or a window changed symbol): redraw every window */
  update(snap) {
    if (snap) this.snap = snap;
    if (this.dragging) return;
    for (const panel of this.app.panels || []) this.draw(panel);
  }

  /** the lines a window should show: [{ kind, pos, price, basis }] */
  wanted(panel) {
    const d = this.snap;
    if (!d || this.app.replay?.active) return [];
    const sym = panel.state.symbol, out = [];
    for (const p of d.positions || []) {
      if (p.grp || !(p.qty > 0)) continue;                 // strategy legs: levels live on the whole strategy
      let basis = null;
      if (p.seg !== "DELTA" && optionChartName(p) === sym) basis = "option";
      else if (p.symbol === sym) basis = "index";
      if (!basis) continue;
      const entry = basis === "option" ? p.avg : p.under_entry;
      if (entry != null) out.push({ kind: "entry", pos: p, price: entry, basis });
      for (const k of ["stop", "target"]) if (p[k]?.basis === basis) out.push({ kind: k, pos: p, price: p[k].price, basis });
    }
    return out;
  }

  title(w, price) {
    const p = w.pos, bot = p.source !== "manual" ? "🤖 " : "";
    if (w.kind === "entry") return `${bot}${p.lots} lot ${p.strike} ${p.type} entry${w.basis === "index" ? " (index)" : ""}`;
    const name = w.kind === "stop" ? "SL" : "TGT";
    if (w.basis === "option") {                            // ₹ at that premium for the whole position (before charges)
      const part = w.kind === "target" && p.target?.lots && p.target.lots < p.lots ? p.target.lots : null;   // "sell half at target"
      const rs = (price - p.avg) * (part ? part * p.lot : p.qty) * (p.mult || 1);
      const lots = part ? ` (${part} lot)` : "";
      return `${name}${lots} ${rs >= 0 ? "+" : "−"}₹${Math.abs(Math.round(rs)).toLocaleString("en-IN")} ⇕`;
    }
    return `${name} idx ⇕`;
  }

  draw(panel) {
    const ws = this.wanted(panel);
    const key = JSON.stringify(ws.map((w) => [w.kind, w.pos.id, w.price, w.pos.target?.lots, w.pos.qty, w.pos.avg]));
    const cur = this.drawn.get(panel);
    if (cur && cur.key === key && cur.series === panel.view.candles) return;
    if (cur && cur.series === panel.view.candles) for (const l of cur.lines) cur.series.removePriceLine(l.line);
    const series = panel.view.candles;
    const lines = ws.map((w) => ({ ...w, line: series.createPriceLine({
      price: w.price, color: COL[w.kind], lineWidth: w.kind === "entry" ? 1 : 2,
      lineStyle: w.kind === "entry" ? LC.LineStyle.Solid : LC.LineStyle.Dashed, axisLabelVisible: true, title: this.title(w, w.price) }) }));
    this.drawn.set(panel, { key, lines, series });
  }

  /** stop / target line under the mouse (within 6 px) */
  near(panel, e) {
    const cur = this.drawn.get(panel);
    if (!cur || panel.drawings.tool !== "cursor" || e.defaultPrevented) return null;
    const y = e.clientY - panel.view.el.getBoundingClientRect().top;
    return cur.lines.find((l) => l.kind !== "entry" && Math.abs(panel.view.priceToY(l.price) - y) < 6) || null;
  }

  attach(panel) {
    const el = panel.view.el;
    el.addEventListener("mousedown", (e) => {
      const l = this.near(panel, e);
      if (!l) return;
      e.stopPropagation(); e.preventDefault();
      this.dragging = { panel, l, from: l.price };
      panel.view.chart.applyOptions({ handleScroll: false, handleScale: false });
    }, true);
    el.addEventListener("mousemove", (e) => {
      const d = this.dragging;
      if (d?.panel === panel) {
        const y = e.clientY - el.getBoundingClientRect().top;
        const price = Math.round(panel.view.yToPrice(y) * 20) / 20;
        if (!isFinite(price) || price <= 0) return;
        d.l.price = price;
        d.l.line.applyOptions({ price, title: this.title(d.l, price) });
      } else el.classList.toggle("dragline", !!this.near(panel, e));
    }, true);
    window.addEventListener("mouseup", () => {
      const d = this.dragging;
      if (d?.panel !== panel) return;
      this.dragging = null;
      panel.view.chart.applyOptions({ handleScroll: true, handleScale: true });
      if (d.l.price !== d.from) this.save(d);
    }, true);
  }

  /** would this level fire at once? (the server checks it the same way every 2 s) */
  firesNow(l, price) {
    const p = l.pos;
    if (l.basis === "option") {
      const px = p.bid ?? p.ltp;
      if (px == null) return false;
      return l.kind === "stop" ? px <= price : px >= price;
    }
    const u = p.under_ltp, up = p.type === "CE";
    if (u == null) return false;
    return l.kind === "stop" ? (up ? u <= price : u >= price) : (up ? u >= price : u <= price);
  }

  async save(d) {
    const { l } = d, p = l.pos, price = l.price;
    const back = () => { l.price = d.from; l.line.applyOptions({ price: d.from, title: this.title(l, d.from) }); };
    if (this.firesNow(l, price)) {
      back();
      this.app.toast(`Not saved: a ${l.kind} at ${fmtPrice(price)} would trigger at once (now ${fmtPrice(l.basis === "option" ? p.bid ?? p.ltp : p.under_ltp)}). Use “Exit” to close now.`, "error");
      return;
    }
    const lv = { basis: l.basis, price, ...(l.kind === "target" && p.target?.lots ? { lots: p.target.lots } : {}) };
    try {
      const r = await fetch(`/api/paper/modify/${p.id}?account=${this.snap.account}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [l.kind]: lv }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || r.statusText);
      p[l.kind] = j[l.kind];
      this.app.toast(`${l.kind === "stop" ? "Stop" : "Target"} moved to ${l.basis === "option" ? "₹" : "index "}${fmtPrice(price)} — ${p.tsym}`, "ok");
      this.app.paper.refresh(true);
    } catch (e) { back(); this.app.toast(`Not saved: ${e.message}`, "error"); }
  }
}
