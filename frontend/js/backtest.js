// Backtest any indicator (built-in or custom) from its BUY/SELL arrows, with intraday rules and costs.
import { fmtPrice, fmtTime } from "./core.js";
import { drawEquity } from "./stats.js";
import { esc } from "./indicators.js";

export class BacktestDialog {
  constructor(app) {
    this.app = app;
    this.dlg = document.getElementById("dlg-backtest");
  }

  open(panel, a) {
    const dlg = this.dlg, m = panel.indicators.meta(a.id);
    dlg.querySelector("h3").textContent = `Backtest · ${m.name} · ${panel.state.symbol} ${panel.state.tf}m`.replace("Dm", " daily");
    const d = (t) => new Date(t * 1000).toISOString().slice(0, 10);
    const last = panel.state.last || Date.now() / 1000;
    const f = dlg.querySelector(".bt-form");
    if (!f.dataset.init) {
      f.querySelector(".bt-from").value = d(last - 365 * 86400);
      f.querySelector(".bt-to").value = d(last);
      f.dataset.init = "1";
    }
    const sym = this.app.symbols.find((x) => x.symbol === panel.state.symbol);
    f.querySelector(".bt-qty").value = sym?.lot || 1;
    dlg.querySelector(".bt-out").innerHTML = `<div class="dim small">Signals = the indicator's ▲ BUY / ▼ SELL arrows. Entry at the signal bar's close,
      one trade at a time, exit at stop / target / 15:15 (never overnight). Results are in index points (an option buyer also pays theta and ~2 pts of costs).</div>`;
    dlg.querySelector(".bt-run").onclick = () => this.run(panel, a);
    dlg.showModal();
  }

  async run(panel, a) {
    const f = this.dlg.querySelector(".bt-form"), v = (c) => f.querySelector(c).value;
    const ts = (s, end) => Math.floor(Date.parse(s + (end ? "T23:59:00Z" : "T00:00:00Z")) / 1000);
    const body = { symbol: panel.state.symbol, tf: panel.state.tf, start: ts(v(".bt-from")), end: ts(v(".bt-to"), true), params: a.params,
                   sl: Number(v(".bt-sl")), tp: Number(v(".bt-tp")), unit: v(".bt-unit"), max_per_day: Number(v(".bt-max")),
                   entry_from: v(".bt-efrom"), entry_to: v(".bt-eto"), side: v(".bt-side"), qty: Number(v(".bt-qty")), cost_pct: Number(v(".bt-cost")) };
    const out = this.dlg.querySelector(".bt-out");
    out.innerHTML = `<div class="dim">Running…</div>`;
    try {
      const r = await fetch(`/api/backtest/${encodeURIComponent(a.id)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const res = await r.json();
      if (!r.ok) throw new Error(res.detail || r.statusText);
      const s = res.summary;
      if (!s.trades) { out.innerHTML = `<div class="dim">No trades — the indicator gave no ▲/▼ signals inside the entry window in this period.</div>`; return; }
      const k = (label, val, cls = "") => `<div class="kpi"><span>${label}</span><b class="${cls}">${val}</b></div>`;
      out.innerHTML = `<div class="kpis">${k("Trades", s.trades)}${k("Win rate", s.win_rate + "%")}${k("Avg pts", s.avg_pts, s.avg_pts >= 0 ? "up" : "down")}
        ${k("Total pts", s.total_pts, s.total_pts >= 0 ? "up" : "down")}${k("₹ (qty " + body.qty + ")", fmtPrice(s.total_rs, 0), s.total_rs >= 0 ? "up" : "down")}
        ${k("Profit factor", s.profit_factor ?? "—")}${k("Avg R", s.avg_R)}${k("Max DD pts", s.max_dd_pts, "down")}${k("Trades/day", s.trades_per_day)}${k("t-stat", s.t_stat ?? "—")}</div>
        <canvas class="equity"></canvas>
        <div class="small ${s.t_stat != null && Math.abs(s.t_stat) < 2 ? "warn" : "dim"}">${s.t_stat != null && Math.abs(s.t_stat) < 2 ? "⚠ t-stat below 2: this result could easily be luck. " : ""}Index points after ${body.cost_pct}% costs per round trip.</div>
        <table><tr><th>Year</th><th>n</th><th>avg pts</th><th>total</th></tr>${Object.entries(s.by_year).map(([y, x]) => `<tr><td>${y}</td><td>${x.n}</td><td class="${x.avg_pts >= 0 ? "up" : "down"}">${x.avg_pts}</td><td>${x.total}</td></tr>`).join("")}</table>
        <div class="table-wrap"><table><tr><th></th><th>Entry</th><th>Dir</th><th>Entry px</th><th>Exit px</th><th>Pts</th><th>R</th><th>Exit</th></tr>${res.trades.slice(-300).reverse().map((t, i) =>
          `<tr><td><button class="bt-view" data-i="${res.trades.length - 1 - i}" title="Replay this trade">▶</button></td><td>${fmtTime(t.entry_time)}</td><td class="${t.dir > 0 ? "up" : "down"}">${t.dir > 0 ? "BUY" : "SELL"}</td>
           <td>${fmtPrice(t.entry)}</td><td>${fmtPrice(t.exit)}</td><td class="${t.pts >= 0 ? "up" : "down"}">${t.pts}</td><td>${t.R}</td><td>${esc(t.reason)}</td></tr>`).join("")}</table></div>`;
      drawEquity(out.querySelector("canvas"), s.equity);
      out.querySelectorAll(".bt-view").forEach((b) => (b.onclick = () => {
        const t = res.trades[Number(b.dataset.i)];
        this.dlg.close();
        this.app.reviewTrade({ symbol: panel.state.symbol, tf: panel.state.tf, entry_time: t.entry_time, exit_time: t.exit_time,
                               side: t.dir > 0 ? "BUY" : "SELL", pnl_rs: t.pts }, panel);
      }));
    } catch (e) { out.innerHTML = `<div class="down">${esc(e.message)}</div>`; }
  }
}
