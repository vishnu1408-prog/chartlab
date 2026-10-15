// Practice panel: paper trades at the last revealed price, with stop / target, intraday square-off at 15:15,
// P&L in points and ₹, your discipline rules, setup tags, and a journal saved on the server.
// Two instruments:
//   Index      — trade the index / stock price itself.
//   ATM option — BUY = buy the at-the-money CE, SELL = buy the ATM PE (option buyer, like real index option trading).
//                Prices: real 1-minute option prices from the dataset when available, else Black-Scholes (India VIX,
//                real time to expiry → theta). Stop / target are in OPTION points.
import { api, store } from "./api.js";
import { PaperEngine, anchorOf, fmtPrice, fmtTime, spanBar, priceAt } from "./core.js";
import { esc } from "./indicators.js";
import { Rules, checkOrder, dayStats } from "./rules.js";

export const TAGS = ["—", "Harmonic", "Breakout", "Pullback", "Reversal", "Range", "News", "Gap", "Other"];

export class PracticePanel {
  constructor(app) {
    this.app = app;
    this.el = document.getElementById("practice");
    this.engine = new PaperEngine({ costPerTrade: 40 });
    this.rules = new Rules(app);
    this.session = Date.now().toString(36);
    this.price = null;          // underlying price
    this.time = null;           // time of the last revealed bar
    this.knownUntil = null;
    this.instrument = store.get("instrument", "index");
    this.contract = null;       // option contract of the open position {type, strike, expiry, source, bars}
    this.meta = null;           // tag / note / symbol of the open position
    this.done = [];             // this session's closed trades with their meta (for chart markers)
    const $ = (s) => this.el.querySelector(s);
    this.$ = $;
    $(".pt-tag").innerHTML = TAGS.map((t) => `<option>${t}</option>`).join("");
    $(".pt-buy").onclick = () => this.order(1);
    $(".pt-sell").onclick = () => this.order(-1);
    $(".pt-close").onclick = () => this.closeAll();
    $(".pt-reset").onclick = () => this.reset();
    $(".pt-cost").onchange = (e) => (this.engine.costPerTrade = Number(e.target.value) || 0);
    $(".pt-journal").onclick = () => this.app.stats.open();
    $(".pt-rules").onclick = () => this.rules.open();
    this.el.querySelectorAll(".pt-inst button").forEach((b) => (b.onclick = () => this.setInstrument(b.dataset.inst)));
    this.setInstrument(this.instrument);
  }

  get optionMode() { return this.instrument === "option"; }
  get lastTime() { return this.optionMode ? (this.knownUntil ?? this.time + 60) - 60 : this.time; }

  reset() {
    if (this.engine.pos) this.closeAll("reset");
    this.engine = new PaperEngine({ costPerTrade: Number(this.$(".pt-cost").value) || 0 });
    this.engine.setSession(anchorOf(this.sym), this.sym?.square_off);
    this.contract = null; this.meta = null; this.done = [];
    this.session = Date.now().toString(36);
    this.render();
  }

  setInstrument(inst) {
    if (this.engine.pos) { this.app.toast("Close the open position before switching", "error"); return; }
    const sym = this.app.symbols?.find((x) => x.symbol === this.app.tradePanel?.state.symbol);
    if (inst === "option" && sym && !sym.options) {
      this.app.toast("Option practice: NIFTY 50, NIFTY BANK or SENSEX", "error");
      inst = "index";
    }
    this.instrument = inst;
    store.set("instrument", inst);
    this.el.querySelectorAll(".pt-inst button").forEach((b) => b.classList.toggle("on", b.dataset.inst === inst));
    this.$(".pt-buy").textContent = inst === "option" ? "BUY CE" : "BUY";
    this.$(".pt-sell").textContent = inst === "option" ? "BUY PE" : "SELL";
    this.$(".lbl-sl").textContent = inst === "option" ? "SL (opt pts)" : "SL pts";
    this.$(".lbl-tp").textContent = inst === "option" ? "Target (opt pts)" : "Target pts";
    this.$(".pt-note").textContent = inst === "option"
      ? "Buys the at-the-money weekly option. Real option prices when the dataset has them, otherwise a Black-Scholes estimate (India VIX, theta included). Fills at the last revealed price ± slippage. Auto square-off 15:15."
      : `Fills at the last revealed price ± slippage. Stop beats target inside one bar. Drag the SL / TP lines on the chart to move them. Auto square-off ${this.engine.sqLabel}.`;
    this.render();
  }

  symbolChanged(sym) {
    this.sym = sym;
    if (!this.engine.pos) this.engine.setSession(anchorOf(sym), sym?.square_off);
    this.setLot(sym?.lot || 1);
    if (this.engine.pos) return;
    this.setInstrument(this.optionMode && sym && !sym.options ? "index" : this.instrument);   // refreshes the square-off note
  }

  setLot(lot) { this.$(".pt-qty").value = lot; }

  setPrice(price, time, knownUntil = null) { this.price = price; this.time = time; this.knownUntil = knownUntil; this.render(); }

  /** current tradable price: underlying, or the option premium of the open contract */
  markPrice() {
    if (!this.optionMode || !this.contract) return this.price;
    return priceAt(this.contract.bars, this.lastTime) ?? this.contract.premium;
  }

  /** discipline guard: returns true when the order may go ahead */
  allowed(qty, slPts) {
    const p = this.engine.pos;
    const msg = checkOrder(this.rules.rules, { time: this.lastTime, qty, sl: slPts, isNewEntry: true, trades: this.engine.trades,
                                               pos: p, unrealized: p ? this.engine.unrealized(this.markPrice()) : 0 });
    if (!msg) return true;
    this.rules.noteBreak(msg);
    this.app.toast(`🛑 Rule: ${msg}`, "error");
    return false;
  }

  async order(side) {
    if (this.price == null) return this.app.toast("No price yet", "error");
    const $ = this.$;
    const qty = Number($(".pt-qty").value);
    const slPts = Number($(".pt-sl").value), tpPts = Number($(".pt-tp").value), slip = Number($(".pt-slip").value) || 0;
    const p = this.engine.pos;
    const opening = !p || (!this.optionMode && p.side === side) || this.optionMode;
    if (opening && !(p && !this.optionMode && p.side !== side) && !this.allowed(qty, slPts)) return;
    const meta = { tag: $(".pt-tag").value, note: $(".pt-note-in").value.trim(), symbol: this.app.tradePanel.state.symbol, blind: this.app.replay.blind };
    try {
      if (!this.optionMode) {
        const fill = this.price + side * slip;
        const closed = this.engine.order(side, qty, fill, this.time, { sl: slPts > 0 ? fill - side * slPts : null, tp: tpPts > 0 ? fill + side * tpPts : null });
        // stops / targets are checked from the NEXT revealed bar on (the fill is at this bar's close)
        this.engine.lastBar = { time: this.time, open: fill, high: fill, low: fill, close: fill };
        this.after(closed);
        if (this.engine.pos && (!p || p.side !== this.engine.pos.side)) this.meta = meta;
        this.app.toast(`${side > 0 ? "BUY" : "SELL"} ${qty} @ ${fmtPrice(fill)}`);
        return this.render();
      }
      if (!this.app.replay.active) return this.app.toast("Option mode works in replay (live option prices need Angel One)", "error");
      const type = side > 0 ? "CE" : "PE";
      if (this.engine.pos) {                         // switching CE↔PE: close the current contract first
        if (this.contract && this.contract.type === type) return this.app.toast(`Already holding the ${type} — Close first to re-enter`, "error");
        this.closeAll("switch");
      }
      const t = this.lastTime;
      const c = await api.option({ symbol: this.app.tradePanel.state.symbol, time: t, type, spot: this.price });
      const fill = c.premium + slip;
      this.contract = c;
      this.meta = { ...meta, contract: `${c.strike} ${c.type} exp ${c.expiry} (${c.source})` };
      this.engine.order(1, qty, fill, t, { sl: slPts > 0 ? fill - slPts : null, tp: tpPts > 0 ? fill + tpPts : null });
      this.engine.lastBar = { time: t, open: fill, high: fill, low: fill, close: fill };
      this.app.toast(`BUY ${qty} ${meta.symbol} ${c.strike} ${type} (exp ${c.expiry}) @ ₹${fmtPrice(fill)} — ${c.source === "real" ? "real price" : "model price"}${c.note ? " · " + c.note : ""}`);
    } catch (e) { this.app.toast(e.message, "error"); }
    this.render();
  }

  closeAll(reason = "manual") {
    const slip = Number(this.$(".pt-slip").value) || 0;
    const p = this.engine.pos;
    if (!p) return;
    const px = this.markPrice() - p.side * slip;
    this.after(this.engine.close(px, this.lastTime, reason));
    this.render();
  }

  /** move the stop / target of the open position (absolute prices: index price, or option premium) */
  setBrackets(sl, tp) {
    const p = this.engine.pos;
    if (!p) return;
    if (sl != null && isFinite(sl)) p.sl = sl;
    if (tp != null && isFinite(tp)) p.tp = tp;
    this.render();
  }

  after(closed) {
    for (const t of closed) {
      const rec = { ...t, ...(this.meta || {}), instrument: this.contract ? `BUY ${this.contract.type}` : "index" };
      this.done.push(rec);
      this.journal(rec);
    }
    if (!this.engine.pos) { this.contract = null; this.meta = null; }
    this.updateMarkers();
  }

  /** a newly revealed underlying bar covering [bar.time, bar.time + spanSec) */
  onBar(bar, spanSec = 60) {
    const lb = this.engine.lastBar;
    if (lb && bar.time < lb.time) return;                       // rewound in replay: never process a bar twice
    if (lb && bar.time === lb.time && !bar.tick) return;
    let b = bar;
    if (this.optionMode && this.contract && this.engine.pos) {
      b = spanBar(this.contract.bars, bar.time, bar.time + spanSec);
      if (!b) { const px = priceAt(this.contract.bars, bar.time + spanSec - 60) ?? this.contract.premium; b = { open: px, high: px, low: px, close: px }; }
      b = { ...b, time: bar.time };
    }
    const done = this.engine.onBar(b);
    for (const t of done) this.app.toast(`Closed (${t.reason}) ${t.pnl_rs >= 0 ? "+" : ""}₹${fmtPrice(t.pnl_rs, 0)}`, t.pnl_rs >= 0 ? "ok" : "error");
    this.after(done);
    // daily loss limit: close the position as soon as realised + open loss reaches it
    const R = this.rules.rules, p = this.engine.pos;
    if (R.enabled && R.dailyLoss && p) {
      const s = dayStats(this.engine.trades, p, b.time, this.engine.unrealized(b.close));
      if (s.pnl <= -R.dailyLoss) { this.closeAll("daily loss limit"); this.app.toast("🛑 Daily loss limit reached — position closed, done for today", "error"); }
    }
  }

  journal(t) {
    const { symbol, tf } = this.app.tradePanel.state;
    api.addJournal({ ...t, symbol: t.symbol || symbol, tf, mode: this.app.replay.active ? "replay" : "live", session: this.session,
                     side: t.instrument && t.instrument !== "index" ? t.instrument : t.side, note: [t.contract, t.note].filter(Boolean).join(" · ") }).catch(() => {});
  }

  /** your entries / exits as markers on the traded chart (they appear only once they happened) */
  updateMarkers() {
    const tp = this.app.tradePanel;
    if (!tp) return;
    for (const q of this.app.panels) q.view.markerSources.trades = [];
    const sym = tp.state.symbol, mk = [];
    const add = (t, entry, exitInfo) => {
      const long = String(t.instrument || t.side).includes("CE") || t.side === "BUY";
      mk.push({ time: t.entry_time, known: t.entry_time, position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown",
                color: "#2962ff", text: t.instrument && t.instrument !== "index" ? t.instrument.replace("BUY ", "") : long ? "B" : "S" });
      if (exitInfo) mk.push({ time: t.exit_time, known: t.exit_time, position: long ? "aboveBar" : "belowBar", shape: "circle",
                              color: t.pnl_rs >= 0 ? "#26a69a" : "#ef5350", text: `${t.pnl_rs >= 0 ? "+" : ""}${Math.round(t.pnl_rs)}` });
    };
    for (const t of this.done) if (t.symbol === sym) add(t, true, true);
    const p = this.engine.pos;
    if (p && this.meta?.symbol === sym) add({ entry_time: p.time, side: p.side > 0 ? "BUY" : "SELL", instrument: this.contract ? `BUY ${this.contract.type}` : "index" }, true, false);
    tp.view.markerSources.trades = mk;
    for (const q of this.app.panels) q.indicators.updateMarkers();
  }

  /** drag the SL / TP lines of an index position on the chart */
  attachDrag(panel) {
    const el = panel.view.el;
    const near = (e) => {
      const p = this.engine.pos;
      if (!p || this.optionMode || this.app.tradePanel !== panel || panel.drawings.tool !== "cursor") return null;
      const y = e.clientY - el.getBoundingClientRect().top;
      for (const k of ["sl", "tp"]) if (p[k] != null && Math.abs(panel.view.priceToY(p[k]) - y) < 6) return k;
      return null;
    };
    el.addEventListener("mousedown", (e) => {
      const k = near(e);
      if (!k) return;
      e.stopPropagation(); e.preventDefault();
      this.dragging = { panel, k };
      panel.view.chart.applyOptions({ handleScroll: false, handleScale: false });
    }, true);
    el.addEventListener("mousemove", (e) => {
      if (this.dragging?.panel === panel) {
        const y = e.clientY - el.getBoundingClientRect().top;
        const price = Math.round(panel.view.yToPrice(y) * 20) / 20;
        if (isFinite(price)) this.setBrackets(this.dragging.k === "sl" ? price : null, this.dragging.k === "tp" ? price : null);
      } else el.classList.toggle("dragline", !!near(e));
    }, true);
    window.addEventListener("mouseup", () => {
      if (this.dragging?.panel !== panel) return;
      this.dragging = null;
      panel.view.chart.applyOptions({ handleScroll: true, handleScale: true });
    }, true);
  }

  render() {
    const $ = this.$, e = this.engine, p = e.pos, c = this.contract;
    const tp = this.app.tradePanel;
    if (!tp) return;
    const v = tp.view;
    for (const q of this.app.panels) if (q !== tp) q.view.clearPriceLines();
    v.clearPriceLines();
    const mark = this.markPrice();
    if (p && !this.optionMode) {
      v.addPriceLine(p.entry, "#d1d4dc", `${p.side > 0 ? "LONG" : "SHORT"} ${p.qty}`);
      if (p.sl != null) v.addPriceLine(p.sl, "#ef5350", "SL ⇕");
      if (p.tp != null) v.addPriceLine(p.tp, "#26a69a", "TP ⇕");
    } else if (p && c) {
      v.addPriceLine(c.spot, c.type === "CE" ? "#26a69a" : "#ef5350", `${c.strike} ${c.type} entry`);
    }
    const u = mark != null ? e.unrealized(mark) : 0;
    const brackets = p ? `<div class="pt-brk">SL <input type="number" step="0.05" class="pt-esl" value="${p.sl ?? ""}"> TP <input type="number" step="0.05" class="pt-etp" value="${p.tp ?? ""}">
        <button class="pt-be" title="Move the stop to your entry price">BE</button></div>` : "";
    if (p && c) {
      $(".pt-pos").innerHTML = `<b class="${c.type === "CE" ? "up" : "down"}">${esc(tp.state.symbol)} ${c.strike} ${c.type}</b> <span class="dim">exp ${esc(c.expiry)} · ${c.source === "real" ? "real price" : "model"}</span><br>
        ${p.qty} @ ₹${fmtPrice(p.entry)} → ₹${fmtPrice(mark)} <span class="${u >= 0 ? "up" : "down"}">${u >= 0 ? "+" : ""}₹${fmtPrice(u, 0)} (${fmtPrice(mark - p.entry)} pts)</span>${brackets}`;
    } else if (p) {
      $(".pt-pos").innerHTML = `<b class="${p.side > 0 ? "up" : "down"}">${p.side > 0 ? "LONG" : "SHORT"} ${p.qty}</b> @ ${fmtPrice(p.entry)}
         <span class="${u >= 0 ? "up" : "down"}">${u >= 0 ? "+" : ""}₹${fmtPrice(u, 0)} (${fmtPrice((mark - p.entry) * p.side)} pts)</span>${brackets}`;
    } else $(".pt-pos").innerHTML = `<span class="dim">Flat</span>`;
    if (p) {
      const el = $(".pt-pos");
      el.querySelector(".pt-esl").onchange = (ev) => this.setBrackets(ev.target.value === "" ? null : Number(ev.target.value), null);
      el.querySelector(".pt-etp").onchange = (ev) => this.setBrackets(null, ev.target.value === "" ? null : Number(ev.target.value));
      el.querySelector(".pt-be").onclick = () => this.setBrackets(p.entry, null);
    }
    const s = e.stats();
    const R = this.rules.rules;
    const ds = this.time != null ? dayStats(e.trades, p, this.lastTime, u) : null;
    const rulesLine = R.enabled && ds ? `<br><span class="dim">Today: ${ds.entries}/${R.maxTrades || "∞"} trades · P&L <span class="${ds.pnl >= 0 ? "up" : "down"}">₹${fmtPrice(ds.pnl, 0)}</span>${R.dailyLoss ? ` / −₹${fmtPrice(R.dailyLoss, 0)} limit` : ""}</span>` : "";
    $(".pt-stats").innerHTML = `Trades <b>${s.trades}</b> · Win <b>${(s.winRate * 100).toFixed(0)}%</b> · Net <b class="${s.net >= 0 ? "up" : "down"}">₹${fmtPrice(s.net, 0)}</b> · Max DD ₹${fmtPrice(s.maxDD, 0)}${rulesLine}`;
    $(".pt-trades").innerHTML = this.done.slice(-30).reverse().map((t) =>
      `<div class="tr-row"><span class="${t.side === "BUY" ? "up" : "down"}">${esc(t.instrument && t.instrument !== "index" ? t.instrument : t.side)}</span> ${t.qty} ${fmtPrice(t.entry)}→${fmtPrice(t.exit)}
       <span class="${t.pnl_rs >= 0 ? "up" : "down"}">${t.pnl_rs >= 0 ? "+" : ""}₹${fmtPrice(t.pnl_rs, 0)}</span><span class="dim"> ${esc(t.reason)}${t.tag && t.tag !== "—" ? " · " + esc(t.tag) : ""} · ${this.app.replay.blind ? "" : fmtTime(t.exit_time)}</span></div>`).join("");
  }
}
