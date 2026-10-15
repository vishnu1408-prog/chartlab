// Option chain side tab: live CE / PE quotes around the money from Angel One (data only — no orders).
// Follows the active chart's symbol; refreshes every 5 s while the tab is visible and the market is open.
import { store } from "./api.js";
import { fmtPrice } from "./core.js";
import { esc } from "./indicators.js";

const SUPPORTED = ["NIFTY 50", "NIFTY BANK", "NIFTY FIN SERVICE", "SENSEX", "MCX GOLD", "MCX SILVER", "MCX CRUDEOIL", "MCX NATURALGAS"];
const num = (v, d = 0) => (v == null ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d }));
const signed = (v) => (v == null ? "" : `${v > 0 ? "+" : ""}${num(v)}`);

export class ChainPanel {
  constructor(app) {
    this.app = app;
    this.el = document.getElementById("tab-chain");
    this.$ = (s) => this.el.querySelector(s);
    this.n = store.get("chainStrikes", 10);
    this.expiry = null; this.symbol = null; this.busy = false;
    this.$(".ch-n").value = String(this.n);
    this.$(".ch-n").onchange = (e) => { this.n = Number(e.target.value); store.set("chainStrikes", this.n); this.refresh(true); };
    this.$(".ch-exp").onchange = (e) => { this.expiry = e.target.value; this.refresh(true); };
    setInterval(() => { if (this.visible && !app.replay.active && this.live) this.refresh(); }, 5000);
    setInterval(() => { if (this.visible && !app.replay.active && !this.live) this.refresh(); }, 60000);
  }

  get visible() { return !document.body.classList.contains("no-side") && document.getElementById("practice").dataset.tab === "chain"; }
  get live() { return document.getElementById("status")?.classList.contains("live"); }

  async refresh(force = false) {
    if (this.busy && !force) return;
    const sym = this.app.active?.state.symbol;
    const body = this.$(".ch-body");
    if (sym !== this.symbol) { this.symbol = sym; this.expiry = null; }
    if (this.app.replay.active) { body.innerHTML = `<div class="dim small">The option chain is live only — leave replay to see it.</div>`; return; }
    if (!SUPPORTED.includes(sym)) {
      body.innerHTML = `<div class="dim small">No option chain for ${esc(sym)}. Available: NIFTY 50, NIFTY BANK, NIFTY FIN SERVICE, SENSEX, MCX GOLD / SILVER / CRUDEOIL / NATURALGAS.</div>`;
      this.$(".ch-head").innerHTML = ""; return;
    }
    this.busy = true;
    try {
      const qs = new URLSearchParams({ symbol: sym, n: this.n, ...(this.expiry ? { expiry: this.expiry } : {}) });
      const r = await fetch(`/api/chain?${qs}`).then((x) => x.json());
      if (sym !== this.app.active?.state.symbol) return;          // symbol changed while loading
      if (r.error || r.detail) { body.innerHTML = `<div class="dim small">${esc(r.error || r.detail)}</div>`; return; }
      this.render(r);
    } catch (e) {
      body.innerHTML = `<div class="dim small">Option chain unavailable: ${esc(e.message)}</div>`;
    } finally { this.busy = false; }
  }

  render(r) {
    this.expiry = r.expiry;
    const sel = this.$(".ch-exp");
    const opts = r.expiries.map((e) => `<option value="${e}"${e === r.expiry ? " selected" : ""}>${new Date(e + "T00:00:00Z").toLocaleDateString("en-IN", { day: "2-digit", month: "short", timeZone: "UTC" })}</option>`).join("");
    if (sel.dataset.k !== opts) { sel.innerHTML = opts; sel.dataset.k = opts; }
    const u = r.underlying;
    this.$(".ch-head").innerHTML = `<b>${esc(u.label)}</b> ${fmtPrice(u.price)} <span class="${(u.chg ?? 0) >= 0 ? "up" : "down"}">${u.chg == null ? "" : signed(Math.round(u.chg * 100) / 100)}</span>
      · ATM straddle <b>${r.straddle == null ? "—" : fmtPrice(r.straddle)}</b> · PCR <b>${r.pcr ?? "—"}</b>
      <span class="dim">· ${r.days_left} d left · lot ${r.lot} · ${r.at}</span>`;
    const maxOI = Math.max(1, ...r.rows.flatMap((x) => [x.ce?.oi || 0, x.pe?.oi || 0]));
    const spot = u.price;
    const bar = (oi, side) => `<i class="ch-bar ${side}" style="width:${Math.round((oi || 0) / maxOI * 100)}%"></i>`;
    const cell = (s, k) => (s ? s[k] : null);
    const rows = r.rows.map((x) => {
      const atm = x.strike === r.atm, ceItm = x.strike < spot, peItm = x.strike > spot;
      const c = x.ce, p = x.pe;
      const ltp = (s) => (s ? `${num(s.ltp, 2)}<small class="${(s.chg ?? 0) >= 0 ? "up" : "down"}">${s.chg == null ? "" : signed(Math.round(s.chg * 10) / 10)}</small>` : "—");
      const ba = (s) => (s && s.bid != null ? `${num(s.bid, 2)} / ${num(s.ask, 2)}` : "");
      return `<tr class="${atm ? "atm" : ""}">
        <td class="ce ${ceItm ? "itm" : ""} oi" title="CE OI ${num(cell(c, "oi"))} lots · volume ${num(cell(c, "vol"))} lots">${bar(cell(c, "oi"), "ce")}<span>${num(cell(c, "oi"))}</span></td>
        <td class="ce ${ceItm ? "itm" : ""} ${(cell(c, "doi") ?? 0) >= 0 ? "up" : "down"}">${signed(cell(c, "doi"))}</td>
        <td class="ce ${ceItm ? "itm" : ""} dim">${cell(c, "iv") ?? "—"}</td>
        <td class="ce ${ceItm ? "itm" : ""} ltp" title="${esc(x.ce_symbol || "")} · bid / ask ${ba(c)}">${ltp(c)}</td>
        <td class="k">${num(x.strike, x.strike % 1 ? 1 : 0)}</td>
        <td class="pe ${peItm ? "itm" : ""} ltp" title="${esc(x.pe_symbol || "")} · bid / ask ${ba(p)}">${ltp(p)}</td>
        <td class="pe ${peItm ? "itm" : ""} dim">${cell(p, "iv") ?? "—"}</td>
        <td class="pe ${peItm ? "itm" : ""} ${(cell(p, "doi") ?? 0) >= 0 ? "up" : "down"}">${signed(cell(p, "doi"))}</td>
        <td class="pe ${peItm ? "itm" : ""} oi" title="PE OI ${num(cell(p, "oi"))} lots · volume ${num(cell(p, "vol"))} lots">${bar(cell(p, "oi"), "pe")}<span>${num(cell(p, "oi"))}</span></td></tr>`;
    }).join("");
    this.$(".ch-body").innerHTML = `<table class="ch-table">
      <thead><tr><th colspan="4" class="ce">CALLS</th><th></th><th colspan="4" class="pe">PUTS</th></tr>
      <tr><th>OI</th><th>ΔOI</th><th>IV</th><th>LTP</th><th>Strike</th><th>LTP</th><th>IV</th><th>ΔOI</th><th>OI</th></tr></thead>
      <tbody>${rows}</tbody></table>
      <div class="dim small">OI, ΔOI and volume in lots. ΔOI = change since ${esc(r.oi_since || "—")} today (when ChartLab first saw it — Angel gives no previous-day OI). IV = estimate from the last price. Shaded = in the money. Hover a price for bid / ask. PCR = put OI ÷ call OI of the strikes shown.</div>`;
  }
}
