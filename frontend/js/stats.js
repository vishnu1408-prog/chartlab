// Trade review & progress statistics from the practice journal.
import { api } from "./api.js";
import { fmtPrice, fmtTime, minuteOfDay } from "./core.js";
import { esc } from "./indicators.js";

/** Pure: KPIs and breakdowns of a list of journal trades (tested in Node). */
export function analyse(trades) {
  const n = trades.length;
  const pnl = trades.map((t) => t.pnl_rs || 0);
  const net = pnl.reduce((a, b) => a + b, 0);
  const wins = pnl.filter((x) => x > 0), losses = pnl.filter((x) => x < 0);
  let eq = 0, peak = 0, dd = 0;
  const equity = pnl.map((x) => { eq += x; peak = Math.max(peak, eq); dd = Math.min(dd, eq - peak); return eq; });
  const group = (key) => {
    const g = {};
    for (const t of trades) { const k = key(t); (g[k] ||= []).push(t.pnl_rs || 0); }
    return Object.entries(g).map(([k, v]) => ({ key: k, n: v.length, win: (v.filter((x) => x > 0).length / v.length) * 100,
                                                 net: v.reduce((a, b) => a + b, 0), avg: v.reduce((a, b) => a + b, 0) / v.length }))
      .sort((a, b) => (a.key < b.key ? -1 : 1));
  };
  const day = (t) => new Date(t.entry_time * 1000).toISOString().slice(0, 10);
  const days = group(day);
  const sumW = wins.reduce((a, b) => a + b, 0), sumL = -losses.reduce((a, b) => a + b, 0);
  return {
    n, net, winRate: n ? (wins.length / n) * 100 : 0, avg: n ? net / n : 0,
    avgWin: wins.length ? sumW / wins.length : 0, avgLoss: losses.length ? -sumL / losses.length : 0,
    pf: sumL > 0 ? sumW / sumL : null, maxDD: dd, equity,
    bestDay: days.length ? Math.max(...days.map((d) => d.net)) : 0, worstDay: days.length ? Math.min(...days.map((d) => d.net)) : 0,
    byTag: group((t) => (t.tag && t.tag !== "—" ? t.tag : "untagged")),
    byHour: group((t) => `${String(Math.floor(minuteOfDay(t.entry_time) / 60)).padStart(2, "0")}:00`),
    byWeekday: group((t) => ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][new Date(t.entry_time * 1000).getUTCDay()]),
    byInstrument: group((t) => t.instrument || (String(t.side).includes("CE") ? "BUY CE" : String(t.side).includes("PE") ? "BUY PE" : "index")),
    byReason: group((t) => t.reason || "?"),
    days,
  };
}

export class StatsDialog {
  constructor(app) {
    this.app = app;
    this.dlg = document.getElementById("dlg-stats");
    this.dlg.querySelectorAll("select").forEach((s) => (s.onchange = () => this.render()));
    this.dlg.querySelector(".st-clear").onclick = async () => { if (confirm("Delete the practice journal? (Paper-account trades are kept with their accounts.)")) { await api.clearJournal(); this.open(); } };
  }

  async open() {
    this.all = await api.journal();
    const sym = this.dlg.querySelector(".st-symbol");
    const syms = [...new Set(this.all.map((t) => t.symbol).filter(Boolean))].sort();
    sym.innerHTML = `<option value="">All symbols</option>` + syms.map((s) => `<option>${esc(s)}</option>`).join("");
    if (!this.dlg.open) this.dlg.showModal();          // visible first: the equity canvas needs its size
    this.render();
  }

  filtered() {
    const mode = this.dlg.querySelector(".st-mode").value, sym = this.dlg.querySelector(".st-symbol").value, scope = this.dlg.querySelector(".st-scope").value;
    return this.all.filter((t) => (!mode || t.mode === mode) && (!sym || t.symbol === sym) && (scope !== "session" || t.session === this.app.practice.session));
  }

  render() {
    const t = this.filtered();
    const a = analyse(t);
    const k = (label, v, cls = "") => `<div class="kpi"><span>${label}</span><b class="${cls}">${v}</b></div>`;
    const rs = (x) => `${x >= 0 ? "+" : "−"}₹${fmtPrice(Math.abs(x), 0)}`;
    this.dlg.querySelector(".st-kpis").innerHTML = !a.n ? `<div class="dim">No trades yet for this filter — practise in replay, they are saved here automatically.</div>` :
      k("Trades", a.n) + k("Win rate", `${a.winRate.toFixed(0)}%`) + k("Net", rs(a.net), a.net >= 0 ? "up" : "down") + k("Avg / trade", rs(a.avg), a.avg >= 0 ? "up" : "down")
      + k("Avg win", rs(a.avgWin), "up") + k("Avg loss", rs(a.avgLoss), "down") + k("Profit factor", a.pf == null ? "—" : a.pf.toFixed(2))
      + k("Max drawdown", rs(a.maxDD), "down") + k("Best day", rs(a.bestDay), "up") + k("Worst day", rs(a.worstDay), "down");
    drawEquity(this.dlg.querySelector("canvas"), a.equity);
    const table = (title, rows) => `<div class="st-group"><h5>${title}</h5><table><tr><th></th><th>n</th><th>win</th><th>net ₹</th><th>avg ₹</th></tr>${rows.map((r) =>
      `<tr><td>${esc(r.key)}</td><td>${r.n}</td><td>${r.win.toFixed(0)}%</td><td class="${r.net >= 0 ? "up" : "down"}">${fmtPrice(r.net, 0)}</td><td class="${r.avg >= 0 ? "up" : "down"}">${fmtPrice(r.avg, 0)}</td></tr>`).join("")}</table></div>`;
    this.dlg.querySelector(".st-groups").innerHTML = a.n ? table("By setup tag", a.byTag) + table("By entry hour", a.byHour) + table("By weekday", a.byWeekday)
      + table("By instrument", a.byInstrument) + table("By exit", a.byReason) : "";
    const rules = this.app.practice.rules.breaks;
    this.dlg.querySelector(".st-rules").textContent = Object.keys(rules).length ? `Rule breaks blocked: ${Object.entries(rules).map(([r, n]) => `${r} ×${n}`).join(" · ")}` : "";
    const tb = this.dlg.querySelector(".st-trades tbody");
    tb.innerHTML = t.slice(-300).reverse().map((x, i) => `<tr><td><button class="st-view" data-i="${t.length - 1 - i}" title="Replay this trade on the chart">▶</button></td>
      <td>${esc(x.symbol || "")}</td><td>${esc(x.mode || "")}</td><td class="${String(x.side).includes("PE") || x.side === "SELL" ? "down" : "up"}">${esc(x.side)}</td>
      <td>${x.qty}</td><td>${x.blind ? "(blind) " : ""}${fmtTime(x.entry_time)}</td><td>${fmtPrice(x.entry)}</td><td>${fmtPrice(x.exit)}</td>
      <td class="${x.pnl_rs >= 0 ? "up" : "down"}">${fmtPrice(x.pnl_rs, 0)}</td><td>${esc(x.reason || "")}</td><td>${esc(x.tag || "")}</td><td class="dim">${esc(x.note || "")}</td></tr>`).join("");
    tb.querySelectorAll(".st-view").forEach((b) => (b.onclick = () => { this.dlg.close(); this.app.reviewTrade(t[Number(b.dataset.i)]); }));
  }
}

export function drawEquity(canvas, eq) {
  const dpr = window.devicePixelRatio || 1, W = canvas.clientWidth, H = canvas.clientHeight;
  canvas.width = W * dpr; canvas.height = H * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!eq.length) return;
  const pts = [0, ...eq], lo = Math.min(0, ...pts), hi = Math.max(0, ...pts), pad = 6;
  const x = (i) => pad + (i / Math.max(1, pts.length - 1)) * (W - 2 * pad);
  const y = (v) => H - pad - ((v - lo) / Math.max(1e-9, hi - lo)) * (H - 2 * pad);
  ctx.strokeStyle = "#363a45"; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.moveTo(pad, y(0)); ctx.lineTo(W - pad, y(0)); ctx.stroke(); ctx.setLineDash([]);
  ctx.strokeStyle = pts[pts.length - 1] >= 0 ? "#26a69a" : "#ef5350"; ctx.lineWidth = 2;
  ctx.beginPath(); pts.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)))); ctx.stroke();
  ctx.fillStyle = "#787b86"; ctx.font = "11px Inter, system-ui, sans-serif"; ctx.textBaseline = "top";
  ctx.fillText(`₹${fmtPrice(hi, 0)}`, pad, 2); ctx.textBaseline = "bottom"; ctx.fillText(`₹${fmtPrice(lo, 0)}`, pad, H - 2);
}
