// Live paper trading tab: NIFTY / SENSEX options at real Angel One prices (no real orders). The server keeps the
// account (cash, positions, orders, history), fills at bid / ask, watches stops / targets every 2 s and squares off at
// 15:15. Two accounts: "My trades" (this ticket) and the Morning Edge bot (automatic, same engine).
import { store } from "./api.js";
import { esc } from "./indicators.js";

const num = (v, d = 2) => (v == null || !isFinite(v) ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d }));
const rs = (v) => (v == null || !isFinite(v) ? "—" : `${v < 0 ? "−" : ""}₹${Math.abs(Math.round(v)).toLocaleString("en-IN")}`);
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const hm = (iso) => (iso ? iso.slice(11, 16) : "");

async function call(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}

export class PaperPanel {
  constructor(app) {
    this.app = app;
    this.el = document.getElementById("tab-paper");
    this.$ = (s) => this.el.querySelector(s);
    this.acct = store.get("paperAcct", "manual");
    this.t = { u: store.get("paperUnder", "NIFTY 50"), type: "CE", expiry: null, strike: null, side: store.get("paperSide", "BUY") };
    this.c = null;            // contract list of the chosen underlying / expiry
    this.snap = null;
    this.seen = new Set();
    this.busy = false;
    const $ = this.$;
    this.el.querySelectorAll(".pp-acct button").forEach((b) => (b.onclick = () => {
      this.acct = b.dataset.acct; store.set("paperAcct", this.acct);
      if (this.acct === "crypto" && !this.btc) this.setUnder("BTC");
      else if (this.acct === "manual" && this.btc) this.setUnder("NIFTY 50");
      this.refresh(true);
    }));
    this.el.querySelectorAll(".pp-und button").forEach((b) => (b.onclick = () => this.setUnder(b.dataset.u)));
    this.el.querySelectorAll(".pp-type button").forEach((b) => (b.onclick = () => { this.t.type = b.dataset.t; this.mark(); this.quote(); }));
    this.el.querySelectorAll(".pp-side button").forEach((b) => (b.onclick = () => {
      this.t.side = b.dataset.s; store.set("paperSide", this.t.side); this.mark(); this.quote();
    }));
    this.el.querySelectorAll(".pp-quick button").forEach((b) => (b.onclick = () => this.quick(Number(b.dataset.m))));
    $(".pp-exp").onchange = (e) => this.loadContracts(e.target.value);
    $(".pp-strike").onchange = (e) => { this.t.strike = Number(e.target.value); this.quote(); };
    $(".pp-otype").onchange = (e) => ($(".pp-limit").style.display = e.target.value === "LIMIT" ? "" : "none");
    $(".pp-lots").oninput = () => this.estimate();
    for (const [b, v] of [[".pp-sb", ".pp-sv"], [".pp-tb", ".pp-tv"]]) {   // remember how levels are set (index / option points…)
      const saved = store.get(`paperBasis${b}`, null);
      if (saved != null && [...$(b).options].some((o) => o.value === saved)) $(b).value = saved;
      $(b).onchange = () => { store.set(`paperBasis${b}`, $(b).value); this.estimate(); };
      $(v).oninput = () => this.estimate();
    }
    $(".pp-buy").onclick = () => this.buy();
    const si = $(".pp-stock");
    const pick = () => {
      const v = si.value.trim().toUpperCase();
      if (!v) return;
      if (this.stocks && !this.stocks.includes(v)) { this.app.toast(`${v}: no stock options (F&O stocks only)`, "error"); return; }
      this.setUnder(v);
    };
    si.addEventListener("change", pick);
    si.addEventListener("keydown", (e) => { if (e.key === "Enter") pick(); });
    fetch("/api/paper/stocks").then((r) => r.json()).then((d) => {
      this.stocks = d.stocks || [];
      document.getElementById("pp-stocks").innerHTML = this.stocks.map((n) => `<option value="${esc(n)}">`).join("");
    }).catch(() => {});
    this.el.addEventListener("click", (e) => this.onClick(e));
    this.el.addEventListener("click", (e) => {
      if (e.target.closest(".pp-chart")) this.app.openOption({ symbol: this.t.u, expiry: this.t.expiry, strike: this.t.strike, type: this.t.type });
    });
    setInterval(() => { if (this.visible && this.snap?.market_open) { this.refresh(); this.quote(); } }, 2000);
    setInterval(() => { if (!this.visible || !this.snap?.market_open) this.refresh(); }, 20000);
  }

  m(v) {                                                 // money in the account's currency
    if (v == null || !isFinite(v)) return "—";
    return this.snap?.currency === "USD" ? `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 2, minimumFractionDigits: 2 })}` : rs(v);
  }

  get btc() { return this.t.u === "BTC"; }
  get selling() { return this.t.side === "SELL" && !this.btc; }
  get mcx() { return this.t.u.startsWith("MCX "); }
  get stock() { return !["NIFTY 50", "SENSEX", "BTC"].includes(this.t.u) && !this.mcx; }
  get uname() { return { "NIFTY 50": "NIFTY", "SENSEX": "SENSEX", "BTC": "BTC", "MCX CRUDEOIL": "CRUDE", "MCX NATURALGAS": "NG" }[this.t.u] || this.t.u; }
  // "index pts" for a stock = rupees on the stock price

  get visible() { return !document.body.classList.contains("no-side") && document.getElementById("practice").dataset.tab === "paper"; }

  mark() {
    this.el.querySelectorAll(".pp-acct button").forEach((b) => b.classList.toggle("on", b.dataset.acct === this.acct));
    this.el.querySelectorAll(".pp-und button").forEach((b) => b.classList.toggle("on", b.dataset.u === this.t.u));
    this.el.querySelectorAll(".pp-type button").forEach((b) => b.classList.toggle("on", b.dataset.t === this.t.type));
    if (this.btc && this.t.side === "SELL") this.t.side = "BUY";           // BTC: option buying only
    const sell = this.selling;
    this.el.querySelectorAll(".pp-side button").forEach((b) => b.classList.toggle("on", b.dataset.s === this.t.side));
    this.$(".pp-side").style.display = this.btc ? "none" : "";
    this.$(".pp-buy").textContent = `${sell ? "SELL" : "BUY"} ${this.uname} ${this.t.strike ?? ""} ${this.t.type}`;
    this.$(".pp-buy").classList.toggle("sell", sell);
    this.$(".pp-otype").style.display = sell ? "none" : "";
    this.$(".pp-half").parentElement.style.display = sell ? "none" : "";
    if (sell) this.$(".pp-limit").style.display = "none";
    this.$(".pp-ticket").style.display = ["manual", "crypto"].includes(this.acct) ? "" : "none";
    const lbl = this.btc || this.mcx || this.stock ? ["ATM", "ITM1", "ITM2", "OTM1"] : ["ATM", "ITM100", "ITM200", "OTM100"];
    const si = this.$(".pp-stock");
    if (document.activeElement !== si) si.value = this.stock ? this.t.u : "";
    si.classList.toggle("on", this.stock);
    this.el.querySelectorAll(".pp-quick button").forEach((b, i) => (b.textContent = lbl[i]));
    this.$(".pp-lots").previousSibling.textContent = this.btc ? "Contracts " : "Lots ";
  }

  setPts(u) {                                           // index-point defaults scale with the index level
    if (this._ptsFor === u) return;
    const pts = { "SENSEX": 115, "BTC": 800, "MCX CRUDEOIL": 30, "MCX NATURALGAS": 3 }[u] ?? 35;
    for (const [b, v] of [[".pp-sb", ".pp-sv"], [".pp-tb", ".pp-tv"]]) if (this.$(b).value === "index_pts") this.$(v).value = pts;
    this._ptsFor = u;
  }

  async setUnder(u, keep = false) {
    this.setPts(u);
    if (u === "BTC") this.acct = "crypto";
    else if (this.acct === "crypto") this.acct = "manual";
    store.set("paperAcct", this.acct);
    this.t.u = u; store.set("paperUnder", u);
    if (!keep) { this.t.expiry = null; this.t.strike = null; }
    this.mark();
    await this.loadContracts(this.t.expiry);
  }

  async loadContracts(expiry) {
    try {
      const qs = new URLSearchParams({ symbol: this.t.u, ...(expiry ? { expiry } : {}) });
      const c = await call("GET", `/api/paper/contracts?${qs}`);
      if (c.error) { this.$(".pp-quote").textContent = c.error; return; }
      this.c = c;
      this.t.expiry = c.expiry;
      this.$(".pp-exp").innerHTML = c.expiries.slice(0, 8).map((e) => `<option value="${e}"${e === c.expiry ? " selected" : ""}>${new Date(e + "T00:00:00Z").toLocaleDateString("en-IN", { day: "2-digit", month: "short", timeZone: "UTC" })}</option>`).join("");
      if (this.t.strike == null || !c.strikes.includes(this.t.strike)) this.t.strike = c.atm ?? c.strikes[Math.floor(c.strikes.length / 2)];
      if (c.stock && c.spot && this._ptsFor !== `${this.t.u}:spot`) {   // stocks: default stop / target = 1 % of the price
        const pts = Math.max(0.05, Math.round(c.spot * 0.01 * 20) / 20);
        for (const [b, v] of [[".pp-sb", ".pp-sv"], [".pp-tb", ".pp-tv"]]) if (this.$(b).value === "index_pts") this.$(v).value = pts;
        this._ptsFor = `${this.t.u}:spot`;
      }
      this.fillStrikes();
      this.mark();
      this.quote();
    } catch (e) { this.$(".pp-quote").textContent = `Contracts unavailable: ${e.message}`; }
  }

  fillStrikes() {
    const c = this.c;
    if (!c) return;
    const i = c.strikes.indexOf(this.t.strike);
    const ks = c.strikes.slice(Math.max(0, i - 30), i + 31);
    this.$(".pp-strike").innerHTML = ks.map((k) => `<option value="${k}"${k === this.t.strike ? " selected" : ""}>${k}${k === c.atm ? " (ATM)" : ""}</option>`).join("");
  }

  quick(m) {
    if (!this.c?.atm) return;
    const sgn = this.t.type === "CE" ? 1 : -1;            // ITM = lower strike for calls, higher for puts
    if (this.btc || this.mcx || this.stock) {             // BTC / MCX / stocks: whole strikes away (ITM1 = one strike ITM)
      const ks = this.c.strikes, i = ks.indexOf(this.c.atm), n = m === 0 ? 0 : m < 0 ? (m === -200 ? 2 : 1) : -1;
      this.t.strike = ks[Math.min(ks.length - 1, Math.max(0, i - sgn * n))];
      this.fillStrikes(); this.mark(); this.quote(); return;
    }
    const want = this.c.atm + sgn * m;
    this.t.strike = this.c.strikes.reduce((a, b) => (Math.abs(b - want) < Math.abs(a - want) ? b : a));
    this.fillStrikes(); this.mark(); this.quote();
  }

  prefill({ symbol, expiry, strike, type }) {
    if (!["NIFTY 50", "SENSEX", "BTC", "MCX CRUDEOIL", "MCX NATURALGAS"].includes(symbol)) { this.app.toast("Paper trading: NIFTY 50, SENSEX, crude oil, natural gas and BTC options", "error"); return; }
    this.acct = "manual"; store.set("paperAcct", "manual");
    this.t = { u: symbol, type, expiry, strike: Number(strike) };
    this.setUnder(symbol, true);
    this.refresh(true);
  }

  async quote() {
    if (!this.t.expiry || this.t.strike == null || !["manual", "crypto"].includes(this.acct)) return;
    try {
      const qs = new URLSearchParams({ symbol: this.t.u, expiry: this.t.expiry, strike: this.t.strike, type: this.t.type });
      const r = await call("GET", `/api/paper/quote?${qs}`);
      this.q = r;
      const q = r.quote || {};
      this.$(".pp-quote").innerHTML = `<button class="pp-chart" title="Open this option's own chart">📈 Chart</button> <b>${esc(r.contract.tsym)}</b> · bid <b>${num(q.bid)}</b> / ask <b>${num(q.ask)}</b> · ${this.btc ? "mark" : "LTP"} ${num(q.ltp)}
        · ${this.mcx ? this.uname + " futures" : this.stock ? this.t.u + " (stock)" : this.uname} <b>${num(r.under)}</b> · ${this.btc ? "1 contract = 0.001 BTC (prices per 1 BTC, $)"
          : this.mcx ? `lot ${r.contract.lot} ${this.t.u === "MCX CRUDEOIL" ? "bbl" : "mmBtu"} · MCX 09:00–23:30, out 23:00` : "lot " + r.contract.lot}`;
      if (this.selling) await this.sellQuote();
      this.estimate();
    } catch (e) { this.$(".pp-quote").textContent = `No quote: ${e.message}`; }
  }

  /** SELL: price the one-leg short through the strategy engine (premium received, margin blocked, delta) */
  async sellQuote() {
    const lots = Math.max(1, Number(this.$(".pp-lots").value) || 1);
    try {
      this.sq = await call("POST", "/api/strategy/quote", { symbol: this.t.u, expiry: this.t.expiry, days_ahead: 0,
        legs: [{ strike: this.t.strike, type: this.t.type, side: "SELL", lots }] });
      this.sq.lotsQuoted = lots;
    } catch (e) { this.sq = { error: e.message }; }
  }

  /** a ticket stop / target as ₹ on the whole short (the strategy engine works in ₹); index levels via delta (≈) */
  sellRs(kind, sel, val) {
    const b = this.$(sel).value, v = Number(this.$(val).value), s = this.sq, l = s?.legs?.[0];
    if (!b || !v || !l) return null;
    const qty = l.lots * l.lot, delta = Math.abs(s.greeks?.delta || 0);
    if (b === "option_pts") return v * qty;
    if (b === "option") { const d = kind === "stop" ? v - l.price : l.price - v; return d > 0 ? d * qty : null; }
    if (b === "index_pts") return delta ? v * delta : null;
    if (b === "index") return delta ? Math.abs(v - s.spot) * delta : null;
    return null;
  }

  estimate() {
    if (this.selling) {
      const s = this.sq, l = s?.legs?.[0];
      if (!s || s.error || !l) { this.$(".pp-est").textContent = s?.error ? `No sell quote: ${s.error}` : "No live price (market closed?) — orders need a live quote."; return; }
      const lots = Math.max(1, Number(this.$(".pp-lots").value) || 1);
      if (lots !== s.lotsQuoted) { this.sellQuote().then(() => this.estimate()); return; }
      const qty = l.lots * l.lot, sl = this.sellRs("stop", ".pp-sb", ".pp-sv"), tg = this.sellRs("target", ".pp-tb", ".pp-tv");
      const approx = (sel) => ["index_pts", "index"].includes(this.$(sel).value) ? "≈" : "";
      this.$(".pp-est").innerHTML = `SELL ${lots} lot = ${qty} qty @ bid ₹${num(l.price)} · premium received ≈ ${rs(l.price * qty)}
        · margin blocked ≈ ${rs(s.margin)} <span class="dim">(${esc(s.basis || "")})</span> · delta ${num(s.greeks?.delta, 0)} ₹/pt
        ${sl || tg ? `<br>stop ${sl ? `${approx(".pp-sb")}−${rs(sl)}` : "none"} · target ${tg ? `${approx(".pp-tb")}+${rs(tg)}` : "none"} <span class="dim">— on the position's P&L (₹)</span>` : ""}
        <br><span class="pp-warn">⚠ A sold option's loss is not limited — the stop is your protection. Out at ${esc(this.snap?.settings?.square_off || "15:15")}.</span>`;
      return;
    }
    const r = this.q;
    if (!r?.buy_1_lot) { this.$(".pp-est").textContent = "No live price (market closed?) — orders need a live quote."; return; }
    const lots = Math.max(1, Number(this.$(".pp-lots").value) || 1);
    const ch = r.charges_buy_1_lot?.total ?? 0;
    const usd = r.currency === "USD", money = (v) => (usd ? `$${v.toFixed(2)}` : rs(v));
    const cash = this.snap && this.snap.currency === r.currency ? money(this.snap.cash) : "";
    this.$(".pp-est").innerHTML = `${usd ? `${lots} contract${lots > 1 ? "s" : ""} = ${(lots * 0.001).toFixed(3)} BTC` : `${lots} lot = ${lots * r.contract.lot} qty`}
      · premium ≈ ${money(r.buy_1_lot * lots)} + buy charges ≈ ${money(ch * lots)}${cash ? ` · cash ${cash}` : ""}${usd && this.snap?.settings?.usd_inr ? ` <span class="dim">(≈ ₹${Math.round(r.buy_1_lot * lots * this.snap.settings.usd_inr).toLocaleString("en-IN")})</span>` : ""}${this.levelPreview(lots)}`;
  }

  /** option-point / option-₹ levels in money terms, at today's ask (the fill moves them with the real fill price) */
  levelPreview(lots) {
    const ask = this.q?.quote?.ask, c = this.q?.contract;
    if (!ask || !c) return "";
    const qty = lots * c.lot * (c.mult || (this.btc ? 0.001 : 1)), out = [];
    for (const [name, b, v, sgn] of [["SL", ".pp-sb", ".pp-sv", -1], ["TGT", ".pp-tb", ".pp-tv", 1]]) {
      const basis = this.$(b).value, x = Number(this.$(v).value);
      if (!x || !["option_pts", "option"].includes(basis)) continue;
      const px = basis === "option_pts" ? ask + sgn * x : x;
      out.push(`${name} ₹${num(px)} (${(px - ask) * qty >= 0 ? "+" : "−"}${this.m(Math.abs((px - ask) * qty)).replace("−", "")})`);
    }
    return out.length ? `<br>${out.join(" · ")} <span class="dim">— from ask ${num(ask)}, fixed at your fill</span>` : "";
  }

  level(sel, val) {
    const b = this.$(sel).value, v = Number(this.$(val).value);
    return b && v ? { basis: b, price: v } : null;
  }

  async buy() {
    const noteBox = this.$(".pp-note");
    if (noteBox.value.trim().length < 3) {
      noteBox.focus(); noteBox.classList.add("need");
      this.app.toast("Write why you are taking this trade first — it is saved with the trade for your review", "error");
      return;
    }
    noteBox.classList.remove("need");
    if (this.selling) return this.sellOpen();
    const lots = Math.max(1, Number(this.$(".pp-lots").value) || 1);
    const target = this.level(".pp-tb", ".pp-tv");
    const half = this.$(".pp-half").checked && lots >= 2;
    if (target && half) target.lots = Math.floor(lots / 2);
    const body = { symbol: this.t.u, expiry: this.t.expiry, strike: this.t.strike, type: this.t.type, side: "BUY", lots,
      order_type: this.$(".pp-otype").value, limit: Number(this.$(".pp-limit").value) || null,
      stop: this.level(".pp-sb", ".pp-sv"), target, be_after_target: half, note: this.$(".pp-note").value.trim() };
    const btn = this.$(".pp-buy");
    btn.disabled = true;
    try {
      const r = await call("POST", `/api/paper/order?account=${this.btc ? "crypto" : "manual"}`, body);
      if (r.status === "filled") this.app.toast(`Paper BUY ${r.lots} lot ${r.tsym} @ ${num(r.fill_price)}`, "ok");
      else if (r.status === "open") this.app.toast(`Limit order placed: ${r.tsym} @ ${num(r.limit)}`);
      else this.app.toast(`Rejected: ${r.reason}`, "error");
      this.$(".pp-note").value = "";
      this.refresh(true);
    } catch (e) { this.app.toast(`Order failed: ${e.message}`, "error"); } finally { btn.disabled = false; }
  }

  /** SELL to open: a one-leg strategy (margin blocked, stop / target in ₹ on its P&L, squared off like everything else) */
  async sellOpen() {
    const btn = this.$(".pp-buy");
    btn.disabled = true;
    try {
      const lots = Math.max(1, Number(this.$(".pp-lots").value) || 1);
      await this.sellQuote();
      if (this.sq?.error) throw new Error(this.sq.error);
      const stop_rs = this.sellRs("stop", ".pp-sb", ".pp-sv"), target_rs = this.sellRs("target", ".pp-tb", ".pp-tv");
      const r = await call("POST", "/api/strategy/order", { symbol: this.t.u, expiry: this.t.expiry, name: `Short ${this.t.strike} ${this.t.type}`,
        legs: [{ strike: this.t.strike, type: this.t.type, side: "SELL", lots }], stop_rs: stop_rs ? Math.round(stop_rs) : null,
        target_rs: target_rs ? Math.round(target_rs) : null, note: this.$(".pp-note").value.trim() });
      if (r.status === "filled") {
        this.app.toast(`Paper SELL ${lots} lot ${this.uname} ${this.t.strike} ${this.t.type} — margin ≈ ${rs(this.sq.margin)} blocked`, "ok");
        this.$(".pp-note").value = "";
      } else this.app.toast(`Rejected: ${r.reason}`, "error");
      this.refresh(true);
    } catch (e) { this.app.toast(`Order failed: ${e.message}`, "error"); } finally { btn.disabled = false; }
  }

  async onClick(e) {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const id = b.dataset.id, a = this.acct;
    try {
      if (b.dataset.act === "exit") await call("POST", `/api/paper/exit/${id}?account=${a}`);
      if (b.dataset.act === "gexit") await call("POST", `/api/strategy/exit/${b.dataset.g}`);
      if (b.dataset.act === "half") await call("POST", `/api/paper/exit/${id}?account=${a}&lots=${b.dataset.lots}`);
      if (b.dataset.act === "cancel") await call("POST", `/api/paper/cancel/${id}?account=${a}`);
      if (b.dataset.act === "review") return this.review(this.snap.trades.find((t) => t.id === id));
      if (b.dataset.act === "chart") {
        const p = this.snap.positions.find((x) => x.id === id);
        return p && this.app.openOption({ symbol: p.symbol, expiry: p.expiry, strike: p.strike, type: p.type });
      }
      if (b.dataset.act === "save") {
        const card = b.closest(".pp-card");
        const lv = (k) => { const bs = card.querySelector(`.${k}-b`).value, v = card.querySelector(`.${k}-v`).value; return bs && v !== "" ? { basis: bs, price: Number(v) } : null; };
        await call("POST", `/api/paper/modify/${id}?account=${a}`, { stop: lv("st"), target: lv("tg") });
        this.app.toast("Stop / target saved", "ok");
      }
      if (b.dataset.act === "set") {
        const box = this.$(".pp-set-body");
        const ch = {};
        box.querySelectorAll("input[data-ch]").forEach((i) => (ch[i.dataset.ch] = Number(i.value) / (i.dataset.pct ? 100 : 1)));
        await call("POST", `/api/paper/settings?account=${a}`, { square_off: box.querySelector(".s-sq").value, max_lots: Number(box.querySelector(".s-ml").value),
          ...(box.querySelector(".s-fx") ? { usd_inr: Number(box.querySelector(".s-fx").value) } : {}),
          ...(box.querySelector(".s-bl") ? { bot_lots: Number(box.querySelector(".s-bl").value) } : {}),
          ...(box.querySelector(".s-t2") ? { me_target2: Number(box.querySelector(".s-t2").value) } : {}),
          ...(box.querySelector(".s-auto") ? { auto_morning_edge: box.querySelector(".s-auto").checked } : {}), charges: ch });
        this.app.toast("Settings saved", "ok");
      }
      if (b.dataset.act === "reset") {
        const cap = Number(this.$(".pp-set-body .s-cap").value) || null;
        if (!confirm(`Reset the ${a === "bot" ? "bot" : "paper"} account${cap ? " to ₹" + cap.toLocaleString("en-IN") : ""}? The old history is archived on disk.`)) return;
        await call("POST", `/api/paper/reset?account=${a}${cap ? "&capital=" + cap : ""}`);
      }
    } catch (err) { this.app.toast(err.message, "error"); }
    this.refresh(true);
  }

  /** open a closed paper trade on its underlying chart: entry / exit marked, the original stop / target as lines */
  review(t) {
    if (!t) return;
    const ep = (iso) => Math.floor(Date.parse(iso + "Z") / 1000);          // IST wall clock = ChartLab chart time
    const sym = t.symbol === "BTC" ? "BTCUSD" : t.symbol;
    const mins = (ep(t.closed) - ep(t.opened)) / 60;
    const p0 = t.plan0 || {}, idx = (lv) => (lv && lv.basis === "index" ? Number(lv.price) : null);
    const usd = this.snap?.currency === "USD", fx = this.snap?.settings?.usd_inr || 88;
    this.app.reviewTrade({
      symbol: sym, tf: mins <= 90 ? "1" : "5", entry_time: ep(t.opened), exit_time: ep(t.closed),
      side: `BUY ${t.type}`, pnl_rs: usd ? t.net * fx : t.net,
      levels: [{ price: t.under_entry, color: "#e040fb", title: "entry" },
               { price: idx(p0.stop), color: "#ef5350", title: "stop (plan)", dash: true },
               { price: idx(p0.target), color: "#26a69a", title: "target (plan)", dash: true },
               { price: t.under_exit, color: "#ffeb3b", title: `exit (${t.why})` }],
    });
  }

  async refresh(force = false) {
    if (this.busy && !force) return;
    this.busy = true;
    try {
      const d = await call("GET", `/api/paper?account=${this.acct}`);
      this.snap = d;
      this.notify(d);
      this.app.posLines.update(d);
      if (this.visible || force) this.render(d);
      if (!this.c && this.visible) this.setUnder(this.t.u, true);
    } catch (e) { this.$(".pp-sum").textContent = `Paper account unavailable: ${e.message}`; } finally { this.busy = false; }
  }

  notify(d) {
    const fresh = d.events.filter((e) => !this.seen.has(e.id));
    const first = this.seen.size === 0;
    d.events.forEach((e) => this.seen.add(e.id));
    if (first) return;
    for (const e of fresh.filter((x) => x.kind !== "ORDER")) {
      this.app.toast(`${["bot", "gann", "plan", "ng"].includes(d.account) ? "🤖 " : ""}${e.text}`, e.kind === "REJECTED" ? "error" : e.kind === "CLOSED" ? "ok" : "");
      if (e.kind !== "NO TRADE" && "Notification" in window && Notification.permission === "granted" && document.hidden)
        new Notification(`Paper ${e.kind}`, { body: e.text, tag: e.id });
    }
  }

  render(d) {
    this.mark();
    const pnlToday = d.today.net + d.unrealized;
    this.$(".pp-sum").innerHTML = `Equity <b>${this.m(d.equity)}</b> · cash ${this.m(d.cash)} · today <b class="${cls(pnlToday)}">${this.m(pnlToday)}</b>
      <span class="dim">(closed ${this.m(d.today.net)} + open ${this.m(d.unrealized)})</span><br>
      All: ${d.all.trades} trades · win ${d.all.win == null ? "—" : Math.round(100 * d.all.win) + "%"} · net <b class="${cls(d.all.net)}">${this.m(d.all.net)}</b>
      · charges ${this.m(d.all.charges)} · <span class="${d.market_open ? "up" : "dim"}">${d.market_open ? "● market open" : "market closed"}</span>
      ${d.status?.error ? `<span class="down"> ⚠ ${esc(d.status.error)}</span>` : ""}`;
    const lvSel = (k, lv) => `<select class="${k}-b"><option value="">none</option>${["index", "option"].map((b) => `<option value="${b}"${lv?.basis === b ? " selected" : ""}>${b === "index" ? "index level" : "option ₹"}</option>`).join("")}</select>
      <input type="number" class="${k}-v" step="0.05" value="${lv?.price ?? ""}">`;
    const posBox = this.$(".pp-pos");
    if (!posBox.contains(document.activeElement)) posBox.innerHTML = (d.positions.length ? `<h5>Open positions</h5>` : `<div class="dim small">No open position.</div>`) + d.positions.map((p) => {
      const short = p.qty < 0, px = (short ? p.ask : p.bid) ?? p.ltp ?? p.avg, pnl = (px - p.avg) * p.qty, pct = (px / p.avg - 1) * (short ? -1 : 1);
      const grp = p.grp && (d.groups || []).find((g) => g.id === p.grp);
      const lv = (x) => (x ? `${x.basis === "index" ? "idx" : "₹"} ${num(x.price)}${x.lots ? ` (${x.lots} lot)` : ""}` : "—");
      if (p.grp) return `<div class="pp-card"><div class="hd"><b>${short ? '<span class="down">SOLD</span> ' : ""}${esc(p.tsym)}</b><b class="${cls(pnl)}">${this.m(pnl)}</b></div>
        <div>${Math.abs(p.lots)} lot · avg ${num(p.avg)} · bid ${num(p.bid)} / ask ${num(p.ask)}</div>
        <div class="dim">🧩 leg of <b>${esc(grp ? grp.name : "strategy")}</b>${grp && grp.pnl != null ? ` · strategy P&L <span class="${cls(grp.pnl)}">${this.m(grp.pnl)}</span>` : ""} —
          stop / target are on the whole strategy${grp && (grp.stop_rs || grp.target_rs) ? ` (stop ${grp.stop_rs ? "−" + rs(grp.stop_rs) : "none"} · target ${grp.target_rs ? "+" + rs(grp.target_rs) : "none"}${grp.margin ? ` · margin ${rs(grp.margin)}` : ""})` : ""}: <a href="strategy.html" target="_blank">🧩 Strategy builder</a></div>
        ${grp && this.acct === "manual" ? `<div class="lv"><button data-act="chart" data-id="${p.id}" title="This option's own chart">📈 Chart</button><button class="x" data-act="gexit" data-g="${grp.id}" title="Close every leg of ${esc(grp.name)} at market">Exit ${esc(grp.name)}</button></div>` : ""}</div>`;
      return `<div class="pp-card"><div class="hd"><b>${esc(p.tsym)}</b><b class="${cls(pnl)}">${this.m(pnl)} (${(100 * pct).toFixed(1)}%)</b></div>
        <div>${p.lots} lot · ${p.qty} qty · avg ${num(p.avg)} · bid ${num(p.bid)} / ask ${num(p.ask)} · index ${num(p.under_ltp)}
        <span class="dim">(entry idx ${num(p.under_entry)}) ${p.source !== "manual" ? "· 🤖" : ""} ${esc(p.note || "")}</span></div>
        <div class="dim">${p.seg === "DELTA" ? `${p.qty} contract(s) = ${(p.qty * 0.001).toFixed(3)} BTC · ` : ""}stop ${lv(p.stop)} · target ${lv(p.target)}${p.be_after_target ? " · then stop to cost" : ""}${p.target2 ? ` · then 2nd target ${lv(p.target2)}` : ""}</div>
        <div class="lv">SL ${lvSel("st", p.stop)} TGT ${lvSel("tg", p.target)} <button data-act="save" data-id="${p.id}">Save</button></div>
        <div class="lv"><button data-act="chart" data-id="${p.id}" title="This option's own chart">📈 Chart</button><button class="x" data-act="exit" data-id="${p.id}">Exit all at bid</button>${p.lots >= 2 ? `<button data-act="half" data-id="${p.id}" data-lots="${Math.floor(p.lots / 2)}">Exit half</button>` : ""}</div></div>`;
    }).join("");
    this.$(".pp-orders").innerHTML = d.orders.length ? `<h5>Pending orders</h5>` + d.orders.map((o) => `<div class="pp-card"><div class="hd">
      <span>${o.side} ${o.lots} lot ${esc(o.tsym)} LIMIT ${num(o.limit)}</span><button data-act="cancel" data-id="${o.id}">Cancel</button></div></div>`).join("") : "";
    const T = [...d.trades].reverse().slice(0, 40);
    const usd = d.currency === "USD", fx = d.settings.usd_inr || 88;
    const sgn = (v) => (v > 0 ? "+" : v < 0 ? "−" : "");
    const money = (v) => `${sgn(v)}${this.m(Math.abs(v)).replace("−", "")}`;
    const inr = (v) => (usd ? ` <span class="dim">(≈ ${sgn(v)}₹${Math.abs(Math.round(v * fx)).toLocaleString("en-IN")})</span>` : "");
    const all = d.trades, wins = all.filter((t) => t.net > 0).length;
    const tot = (k) => all.reduce((a, t) => a + (t[k] || 0), 0);
    this.$(".pp-trades").innerHTML = T.length ? `<h5>Closed trades</h5>
      <div class="pp-card"><b>${all.length} trade${all.length > 1 ? "s" : ""}</b> · win ${Math.round(100 * wins / all.length)}% ·
        gross <b class="${cls(tot("gross"))}">${money(tot("gross"))}</b> · charges ${this.m(tot("charges"))} ·
        net <b class="${cls(tot("net"))}">${money(tot("net"))}</b>${inr(tot("net"))}
        <span class="dim"> · all trades are also in 📊 Stats (filter “Paper”)</span></div>` +
      T.map((t) => {
        const qty = t.qty ?? (t.lots || 1) * (t.symbol === "SENSEX" ? 20 : t.symbol === "BTC" ? 1 : 65);
        const ss = t.side === "SHORT" ? -1 : 1, pts = (t.exit - t.entry) * ss, pct = t.entry ? pts / t.entry : 0;
        const unit = t.symbol === "BTC" ? `contract${qty > 1 ? "s" : ""}` : "qty";
        return `<div class="pp-card" title="${esc(t.note || "")}"><div class="hd"><span>${t.side === "SHORT" ? '<span class="down">SOLD</span> ' : ""}${t.grp ? "🧩 " : ""}<b>${esc(t.tsym)}</b>
          <span class="dim">${esc(t.opened.slice(5, 10))} ${hm(t.opened)}→${hm(t.closed)} · ${qty} ${unit} · ${esc(t.why)}</span></span>
          <b class="${cls(t.net)}">${money(t.net)}</b></div>
          <div>${num(t.entry)} → ${num(t.exit)} <span class="${cls(pts)}">(${sgn(pts)}${num(Math.abs(pts))} pts, ${sgn(pct)}${Math.abs(100 * pct).toFixed(1)}%)</span>
          · gross <span class="${cls(t.gross)}">${money(t.gross)}</span> · charges ${this.m(t.charges)} · net <b class="${cls(t.net)}">${money(t.net)}</b>${inr(t.net)}</div>
          ${t.note ? `<div class="dim">${esc(t.note)}</div>` : ""}
          <div class="lv"><button data-act="review" data-id="${esc(t.id)}" title="Open this trade on its chart (replay stops at the exit — step forward to see what came next)">🔍 Review on chart</button></div></div>`;
      }).join("") : "";
    const s = d.settings, c = s.charges;
    const box = this.$(".pp-set-body");
    if (!box.contains(document.activeElement)) {
      box.innerHTML = `<div class="pp-row"><label>Square-off <input type="text" class="s-sq" value="${esc(s.square_off)}" size="5"></label>
        <label>Max lots <input type="number" class="s-ml" value="${s.max_lots}"></label></div>
        ${["bot", "gann", "plan", "ng"].includes(d.account) ? `<div class="pp-row">${d.account === "bot" ? `<label><input type="checkbox" class="s-auto"${s.auto_morning_edge ? " checked" : ""}> trade Morning Edge signals</label>` : ""}
          <label>Lots <input type="number" class="s-bl" value="${s.bot_lots}"></label></div>` : ""}
        ${["bot", "plan"].includes(d.account) ? `<div class="pp-row"><label title="After lot 1 hits its target and the stop moves to cost, the rest also gets a target this many NIFTY points from entry. 0 = off (ride to the stop or 15:15). Backtest: 70 pts earned less (+1.3 vs +3.2 pts/lot).">Morning Edge 2nd target <input type="number" class="s-t2" min="0" step="5" value="${s.me_target2 || 0}"> idx pts (0 = off)</label></div>` : ""}
        ${d.currency === "USD" ? `<div class="pp-row">Fee % of BTC value <input type="number" step="0.001" data-ch="fee_rate" data-pct="1" value="${+(c.fee_rate * 100).toFixed(4)}">
          cap % of premium <input type="number" step="0.1" data-ch="fee_cap" data-pct="1" value="${+(c.fee_cap * 100).toFixed(2)}"></div>
        <div class="pp-row">GST % <input type="number" data-ch="gst" data-pct="1" value="${+(c.gst * 100).toFixed(1)}">
          USD→INR <input type="number" step="0.1" class="s-fx" value="${s.usd_inr}"></div>` : `        <div class="pp-row">Brokerage ₹/order <input type="number" data-ch="brokerage" value="${c.brokerage}"></div>
        <div class="pp-row">STT on sell % <input type="number" step="0.001" data-ch="stt_sell" data-pct="1" value="${+(c.stt_sell * 100).toFixed(4)}">
          Exch NFO % <input type="number" step="0.0001" data-ch="exch_nfo" data-pct="1" value="${+(c.exch_nfo * 100).toFixed(5)}"></div>
        <div class="pp-row">Exch BFO % <input type="number" step="0.0001" data-ch="exch_bfo" data-pct="1" value="${+(c.exch_bfo * 100).toFixed(5)}">
          Stamp buy % <input type="number" step="0.001" data-ch="stamp_buy" data-pct="1" value="${+(c.stamp_buy * 100).toFixed(4)}"></div>
        <div class="pp-row">GST % <input type="number" data-ch="gst" data-pct="1" value="${+(c.gst * 100).toFixed(1)}">
          SEBI % <input type="number" step="0.00001" data-ch="sebi" data-pct="1" value="${+(c.sebi * 100).toFixed(5)}"></div>
`}
        <div class="pp-row"><button data-act="set">Save settings</button></div>
        <div class="pp-row">Reset with capital ₹ <input type="number" class="s-cap" value="${s.capital}"><button data-act="reset" class="x">Reset account</button></div>
        <div class="dim">Rates change — check ${d.currency === "USD" ? "Delta Exchange's fee page" : "Angel One's charge sheet"}. Fills: buy at the best ask, sell at the best bid. Single options: buying only — spreads / straddles with sold legs: 🧩 Strategy builder.
        Stops / targets are checked every 2 s; anything open is sold at the square-off time, or at the first live price if ChartLab was off.</div>`;
    }
  }
}
