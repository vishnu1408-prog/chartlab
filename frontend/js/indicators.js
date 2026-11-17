// Indicator manager: add / configure / hide / remove, compute on the server, render series + markers + shapes.
// Sub-pane indicators (RSI, MACD, custom non-overlay) each get their own pane below the price chart.
import { api } from "./api.js";
import { visibleAt, lastLE } from "./core.js";

const LC = globalThis.window?.LightweightCharts;   // undefined under Node (tests import pure helpers from here)

export class IndicatorManager {
  constructor(view, legendEl, { toast, active, onSave, onNewMarker, onBacktest }) {
    this.view = view;
    this.legendEl = legendEl;
    this.toast = toast;
    this.onSave = onSave || (() => {});
    this.onNewMarker = onNewMarker || (() => {});
    this.onBacktest = onBacktest || (() => {});
    this.lastSeen = {};            // uid → time of the newest visible marker (for alerts)
    this.catalog = [];
    this.errors = [];
    this.active = active || [];
    this.results = {};             // uid → server output
    this.series = {};              // uid → [{api, data, def}]
    this.replay = false;
    view.layers.push((ctx) => this.renderShapes(ctx));
  }

  setCatalog(catalog, errors = []) {
    this.catalog = catalog;
    this.errors = errors;
    // drop saved indicators that no longer exist — but never wipe the user's setup because the list came back empty
    if (this.catalog.length) this.active = this.active.filter((a) => this.meta(a.id));
  }

  meta(id) { return this.catalog.find((m) => m.id === id); }
  config() { return this.active.map(({ uid, id, params, visible, alert }) => ({ uid, id, params, visible, alert: !!alert })); }
  save() { this.onSave(); }

  add(id) {
    const uid = Math.random().toString(36).slice(2, 9);
    this.active.push({ uid, id, params: {}, visible: true });
    this.save();
    return this.refresh();
  }

  remove(uid) {
    this.dropSeries(uid);
    this.active = this.active.filter((a) => a.uid !== uid);
    delete this.results[uid];
    this.save();
    this.relayoutPanes();
    this.updateMarkers(); this.renderLegend(); this.view.requestDraw();
  }

  toggle(uid) {
    const a = this.active.find((x) => x.uid === uid);
    a.visible = !a.visible;
    this.save();
    for (const s of this.series[uid] || []) s.api.applyOptions({ visible: a.visible });
    this.updateMarkers(); this.renderLegend(); this.view.requestDraw();
  }

  setParams(uid, params) {
    const a = this.active.find((x) => x.uid === uid);
    a.params = params;
    this.save();
    return this.refresh([uid]);
  }

  /** symbol change: remove every indicator's old lines at once so they can't distort the new chart's price scale */
  clearAll() {
    for (const uid of Object.keys(this.series)) this.dropSeries(uid);
    this.results = {};
    this.lastSeen = {};
    this.view.setMarkers([]);
  }

  dropSeries(uid) {
    for (const s of this.series[uid] || []) { try { this.view.chart.removeSeries(s.api); } catch { /* already gone */ } }
    delete this.series[uid];
  }

  paneOf(a) {
    const m = this.meta(a.id);
    if (!m || m.overlay) return 0;
    const subs = this.active.filter((x) => !this.meta(x.id)?.overlay);
    return 1 + subs.findIndex((x) => x.uid === a.uid);
  }

  relayoutPanes() {
    // removing a sub-pane indicator: rebuild the others so pane indices stay contiguous
    for (const a of this.active) if (!this.meta(a.id)?.overlay && this.results[a.uid]) { this.dropSeries(a.uid); this.build(a); }
    const panes = this.view.chart.panes();
    for (let i = panes.length - 1; i >= 1; i--) {
      if (!this.active.some((a) => this.paneOf(a) === i) && this.view.chart.removePane) { try { this.view.chart.removePane(i); } catch { /* ignore */ } }
    }
  }

  /** Recompute (all, or the given uids) for the bars currently loaded. */
  async refresh(uids = null) {
    const v = this.view;
    if (!v.bars.length) return;
    const ctx = this.context;
    const body = { symbol: ctx.symbol, tf: ctx.tf, start: ctx.start ?? v.bars[0].time, end: ctx.end ?? v.bars[v.bars.length - 1].time };
    const todo = this.active.filter((a) => (!uids || uids.includes(a.uid)) && this.meta(a.id));
    await Promise.all(todo.map(async (a) => {
      try {
        const out = await api.compute(a.id, { ...body, params: a.params });
        if (out.error) throw new Error(out.error);
        this.results[a.uid] = out;
        this.dropSeries(a.uid);
        this.build(a);
      } catch (e) {
        this.results[a.uid] = { series: [], markers: [], shapes: [], info: { error: e.message } };
        this.toast(`${this.meta(a.id)?.name || a.id}: ${e.message}`, "error");
      }
    }));
    this.updateMarkers();
    this.renderLegend();
    v.requestDraw();
  }

  build(a) {
    const out = this.results[a.uid];
    const pane = this.paneOf(a);
    const list = [];
    for (const s of out.series) {
      const opts = { color: s.color, lineWidth: s.lineWidth || 2, lineStyle: s.lineStyle || 0, priceLineVisible: false, lastValueVisible: s.type !== "histogram",
                     visible: a.visible, title: "", crosshairMarkerVisible: false };
      const kind = s.type === "histogram" ? LC.HistogramSeries : s.type === "area" ? LC.AreaSeries : LC.LineSeries;
      const api_ = this.view.chart.addSeries(kind, opts, s.pane === 0 ? 0 : pane);
      list.push({ api: api_, data: s.data, def: s, shown: -1 });
    }
    this.series[a.uid] = list;
    this.applyCursor(true);
  }

  /** Show indicator values up to the replay cursor (all of them when not replaying). */
  applyCursor(full = false) {
    const ku = this.replay ? this.view.knownUntil : null;
    const tfSec = this.view.tfSec;
    for (const list of Object.values(this.series)) {
      for (const s of list) {
        // a value at bar t is known when that bar has closed: t + tfSec <= knownUntil
        const n = ku == null ? s.data.length : lastLE(s.data.map((d) => d.time), ku - tfSec) + 1;
        if (!full && n === s.shown + 1 && n > 0) s.api.update(s.data[n - 1]);       // forward step: cheap update
        else if (n !== s.shown || full) s.api.setData(s.data.slice(0, n));
        s.shown = n;
      }
    }
    this.updateMarkers();
    this.updateLegendValues();
  }

  updateMarkers() {
    const ku = this.replay ? this.view.knownUntil : null;
    const tfSec = this.view.tfSec;
    let all = [];
    for (const a of this.active) {
      if (!a.visible || !this.results[a.uid]) continue;
      const mine = visibleAt(this.results[a.uid].markers || [], ku, this.replay, tfSec).filter((m) => ku == null || m.time + tfSec <= ku);
      // alerts: a signal marker newer than the last one seen in this window
      const sig = mine.filter((m) => m.shape === "arrowUp" || m.shape === "arrowDown");
      const newest = sig.length ? sig[sig.length - 1] : null;
      const prev = this.lastSeen[a.uid];
      if (newest && prev !== undefined && newest.time > (prev ?? -Infinity)) this.onNewMarker(a, this.meta(a.id)?.name || a.id, newest);
      this.lastSeen[a.uid] = newest ? newest.time : null;
      all = all.concat(mine);
    }
    // news / practice-trade markers: known at their own time (not bar close)
    const times = this.view.times;
    for (const m of this.view.extraMarkers || []) {
      if (ku != null && (m.known ?? m.time) > ku) continue;
      const i = lastLE(times, m.time);
      if (i >= 0) all.push({ ...m, time: times[i] });
    }
    all.sort((x, y) => x.time - y.time);
    this.view.setMarkers(all.map(({ time, position, shape, color, text }) => ({ time, position, shape, color, text })));
  }

  renderShapes(ctx) {
    for (const a of this.active) {
      const out = this.results[a.uid];
      if (a.visible && out && out.shapes && out.shapes.length) this.view.drawShapes(ctx, out.shapes, this.replay);
    }
  }

  renderLegend() {
    const el = this.legendEl;
    el.innerHTML = "";
    for (const a of this.active) {
      const m = this.meta(a.id);
      const out = this.results[a.uid] || {};
      const p = Object.entries(out.params || {}).filter(([, v]) => typeof v === "number").map(([, v]) => v).slice(0, 3).join(", ");
      const row = document.createElement("div");
      row.className = "legend-row" + (a.visible ? "" : " hidden");
      row.dataset.uid = a.uid;
      row.innerHTML = `<span class="lg-name">${esc(m?.name || a.id)}${p ? ` <span class="dim">${esc(p)}</span>` : ""}</span><span class="lg-val"></span>
        <button title="Settings" data-act="cfg">⚙</button><button title="Show / hide" data-act="eye">${a.visible ? "👁" : "◌"}</button>
        <button title="Alert on new signals" data-act="bell" class="${a.alert ? "on-bell" : ""}">${a.alert ? "🔔" : "🔕"}</button>
        <button title="Backtest this indicator" data-act="bt">⚗</button>
        <button title="Remove" data-act="del">✕</button>`;
      const info = out.info || {};
      // summaries are computed over the whole loaded window → hidden in replay (they would show the future)
      if (Object.keys(info).length && (!this.replay || info.error)) {
        const s = document.createElement("div");
        s.className = "lg-info" + (info.error ? " err" : "");
        s.textContent = Object.entries(info).map(([k, v]) => `${k}: ${v}`).join(" · ");
        row.appendChild(s);
      }
      row.querySelector('[data-act="cfg"]').onclick = () => this.openSettings(a);
      row.querySelector('[data-act="eye"]').onclick = () => this.toggle(a.uid);
      row.querySelector('[data-act="del"]').onclick = () => this.remove(a.uid);
      row.querySelector('[data-act="bell"]').onclick = () => { a.alert = !a.alert; this.save(); this.renderLegend(); if (a.alert) this.toast(`Alerts on for ${m?.name || a.id}: you get a notification on each new signal`); };
      row.querySelector('[data-act="bt"]').onclick = () => this.onBacktest(a);
      el.appendChild(row);
    }
    this.updateLegendValues();
  }

  /** current value of each indicator at the replay cursor (or the latest bar) next to its name */
  updateLegendValues() {
    for (const row of this.legendEl.querySelectorAll(".legend-row")) {
      const list = this.series[row.dataset.uid] || [];
      const s = list[0];
      const span = row.querySelector(".lg-val");
      if (!span) continue;
      if (!s || !s.shown) { span.textContent = ""; continue; }
      let v = null;
      for (let i = Math.min(s.shown, s.data.length) - 1; i >= 0 && i >= s.shown - 50; i--) if (s.data[i].value != null) { v = s.data[i].value; break; }
      span.textContent = v == null ? "" : ` ${Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(2)}${s.def.title.includes("%") ? "%" : ""}`;
      span.style.color = s.def.color;
    }
  }

  // ── dialogs ──
  openPicker() {
    const dlg = document.getElementById("dlg-indicators");
    const list = dlg.querySelector(".list");
    const search = dlg.querySelector("input");
    const draw = () => {
      const q = search.value.toLowerCase();
      list.innerHTML = "";
      for (const src of ["builtin", "custom"]) {
        const items = this.catalog.filter((m) => m.source === src && (m.name + m.description).toLowerCase().includes(q));
        if (!items.length) continue;
        const h = document.createElement("div"); h.className = "list-head"; h.textContent = src === "builtin" ? "Built-in" : "Custom (custom_indicators/)";
        list.appendChild(h);
        for (const m of items) {
          const b = document.createElement("button");
          b.className = "list-item";
          b.innerHTML = `<b>${esc(m.name)}</b><span>${esc(m.description || "")}</span>`;
          b.onclick = () => { dlg.close(); this.add(m.id); };
          list.appendChild(b);
        }
      }
      for (const e of this.errors) {
        const d = document.createElement("div"); d.className = "list-err"; d.textContent = `⚠ ${e.file}: ${e.error.split("\n").slice(-2).join(" ")}`;
        list.appendChild(d);
      }
    };
    search.value = ""; search.oninput = draw; draw();
    dlg.showModal(); search.focus();
  }

  openSettings(a) {
    const m = this.meta(a.id);
    const dlg = document.getElementById("dlg-settings");
    dlg.querySelector("h3").textContent = m.name;
    const form = dlg.querySelector(".form");
    form.innerHTML = "";
    const cur = { ...(this.results[a.uid]?.params || {}), ...a.params };
    let group = null;
    for (const p of m.params) {
      if (p.group && p.group !== group) { group = p.group; const g = document.createElement("div"); g.className = "form-group"; g.textContent = group; form.appendChild(g); }
      const row = document.createElement("label");
      row.className = "form-row";
      const val = cur[p.name] ?? p.default;
      let input;
      if (p.type === "bool") { input = document.createElement("input"); input.type = "checkbox"; input.checked = !!val; }
      else if (p.type === "choice") { input = document.createElement("select"); for (const o of p.options) { const op = new Option(o, o); op.selected = o === val; input.add(op); } }
      else { input = document.createElement("input"); input.type = p.type === "color" ? "color" : p.type === "text" ? "text" : "number";
             input.value = val; if (p.step) input.step = p.step; if (p.min != null) input.min = p.min; if (p.max != null) input.max = p.max; }
      input.name = p.name;
      row.innerHTML = `<span>${esc(p.label || p.name)}</span>`;
      row.appendChild(input);
      form.appendChild(row);
    }
    dlg.querySelector(".apply").onclick = () => {
      const params = {};
      for (const p of m.params) {
        const el = form.querySelector(`[name="${CSS.escape(p.name)}"]`);
        params[p.name] = p.type === "bool" ? el.checked : p.type === "int" || p.type === "float" ? Number(el.value) : el.value;
      }
      dlg.close();
      this.setParams(a.uid, params);
    };
    dlg.querySelector(".defaults").onclick = () => { dlg.close(); this.setParams(a.uid, {}); };
    dlg.showModal();
  }
}

export const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
