// News side panel: live market headlines (RSS) and historical headlines (GDELT) shown at the replay clock —
// in replay you only see what had been published by then. Optional markers on the charts, a history downloader and
// the "News impact" study (how NIFTY moved after headlines with a keyword).
import { api, store } from "./api.js";
import { fmtTime, fmtPrice, bucketStart, TF_SECONDS } from "./core.js";
import { esc } from "./indicators.js";

const req = async (method, url) => { const r = await fetch(url, { method }); const d = await r.json(); if (!r.ok) throw new Error(d.detail || r.statusText); return d; };
const q = (o) => Object.entries(o).filter(([, v]) => v != null && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");

export class NewsPanel {
  constructor(app) {
    this.app = app;
    this.el = document.getElementById("tab-news");
    this.list = this.el.querySelector(".nw-list");
    this.onChart = store.get("newsOnChart2", false);                 // off by default: busy days have news in every candle
    this.lastK = null; this.lastAt = 0; this.items = [];
    const $ = (s) => this.el.querySelector(s);
    $(".nw-q").oninput = () => { clearTimeout(this._t); this._t = setTimeout(() => this.refresh(true), 300); };
    const tg = $(".nw-chart"); tg.checked = this.onChart;
    tg.onchange = () => {
      this.onChart = tg.checked; store.set("newsOnChart2", this.onChart); this.refresh(true);
      if (this.onChart && !$(".nw-q").value.trim()) app.toast("Tip: type a keyword in the news filter (e.g. RBI) to mark only those headlines on the chart");
    };
    $(".nw-impact").onclick = () => this.openImpact();
    $(".nw-history").onclick = () => this.openHistory();
    setInterval(() => { if (!app.replay.active && this.visible) this.refresh(); }, 60000);
    setInterval(() => this.pollStatus(), 5000);
  }

  get visible() { return !document.body.classList.contains("no-side") && document.getElementById("practice").dataset.tab === "news"; }

  /** called on every replay render / live update; throttled */
  onClock(K) {
    if (K == null) return;
    if (this.lastK != null && Math.abs(K - this.lastK) < 300 && Date.now() - this.lastAt < 4000) return;
    this.refresh();
  }

  async refresh(force = false) {
    const rp = this.app.replay;
    const K = rp.active ? rp.knownUntil : null;
    this.lastK = K; this.lastAt = Date.now();
    if (rp.blind) {
      this.list.innerHTML = `<div class="dim small">News is hidden in blind mode (headlines would give the date away).</div>`;
      this.setMarkers([]);
      return;
    }
    const text = this.el.querySelector(".nw-q").value.trim();
    try {
      this.items = await req("GET", `/api/news?${q({ end: K, start: K != null ? K - 4 * 86400 : null, q: text, limit: 200 })}`);
    } catch (e) { this.list.innerHTML = `<div class="dim small">News unavailable: ${esc(e.message)}</div>`; return; }
    const newest = this.items[0]?.time;
    this.list.innerHTML = this.items.length ? this.items.map((n) => `<a class="nw-item${K != null && K - n.time < 900 ? " fresh" : ""}" href="${esc(n.url || "#")}" target="_blank" rel="noopener noreferrer">
        <span class="nw-time">${fmtTime(n.time)}</span> <span class="nw-src">${esc(n.source || "")}</span><span class="nw-title">${esc(n.title)}</span></a>`).join("")
      : `<div class="dim small">${K != null ? "No headlines cached for these days yet — they download in the background (GDELT allows one request every few seconds)." : "No headlines yet."}</div>`;
    this.newest = newest;
    if (this.onChart) await this.markers(K, text); else this.setMarkers([]);
  }

  async markers(K, text) {
    const panels = this.app.panels.filter((p) => p.view.bars.length);
    if (!panels.length) return;
    const first = Math.min(...panels.map((p) => p.view.bars[0].time));
    let items;
    try { items = await req("GET", `/api/news?${q({ start: first, end: K, q: text, limit: 2000 })}`); } catch { return; }
    for (const p of panels) {
      const tf = p.state.tf, buckets = new Map(), times = p.view.times;
      for (const n of items) {
        const mod = (n.time % 86400) / 60;
        if (tf !== "D" && (mod < 540 || mod >= 930)) continue;                  // intraday charts: market-hours news only
        const b = tf === "D" ? bucketStart(n.time, tf) : Math.max(bucketStart(n.time, tf), bucketStart(Math.floor(n.time / 86400) * 86400 + 555 * 60, tf));
        if (!times.length || b < times[0]) continue;
        const e = buckets.get(b) || { time: b, known: n.time, n: 0 };
        e.n++; e.known = Math.min(e.known, n.time);
        buckets.set(b, e);
      }
      // with a keyword: mark every matching candle; without: only candles with a burst of news (≥ 3 headlines)
      const min = text ? 1 : 3;
      p.view.markerSources.news = [...buckets.values()].filter((e) => e.n >= min).map((e) => ({ time: e.time, known: e.known, position: "aboveBar",
                                                                       shape: "square", color: "#f5a623", text: e.n > 1 ? `📰${e.n}` : "📰" }));
      p.indicators.updateMarkers();
    }
  }

  setMarkers(list) { for (const p of this.app.panels) { p.view.markerSources.news = list; p.indicators.updateMarkers(); } }

  /** replay started: download (in the background) the headlines of the replayed days */
  async ensureRange(startT, endT, nowT) {
    if (startT == null || endT == null) return;
    const d = (t) => new Date(t * 1000).toISOString().slice(0, 10);
    try {
      await req("POST", `/api/news/ensure?${q({ start: d(startT), end: d(endT) })}`);
      if (nowT != null) await req("POST", `/api/news/ensure?${q({ start: d(nowT - 3 * 86400), end: d(nowT), priority: true })}`);
    } catch { /* offline */ }
    this.pollStatus();
  }

  async pollStatus() {
    try {
      const s = await req("GET", "/api/news/status");
      const busy = s.queued || s.current;
      this.el.querySelector(".nw-status").textContent = `${s.headlines.toLocaleString("en-IN")} headlines · ${s.days_cached} past days cached`
        + (busy ? ` · downloading ${s.current?.day || ""} (${s.queued} left)` : "") + (s.gdelt_error && busy ? ` · ${s.gdelt_error}` : "");
      if (busy && this.app.replay.active && Date.now() - this.lastAt > 8000) this.refresh();
    } catch { /* ignore */ }
  }

  openHistory() {
    const dlg = document.getElementById("dlg-history");
    const today = new Date().toISOString().slice(0, 10);
    const a = dlg.querySelector(".hs-from"), b = dlg.querySelector(".hs-to");
    if (!a.value) a.value = new Date(Date.now() - 90 * 86400e3).toISOString().slice(0, 10);
    if (!b.value) b.value = today;
    dlg.querySelector(".apply").onclick = async () => {
      try {
        const r = await req("POST", `/api/news/ensure?${q({ start: a.value, end: b.value })}`);
        this.app.toast(`Downloading headlines for ${r.queued} days in the background (≈ ${Math.ceil(r.queued * 7 / 60)} min)`);
        dlg.close();
      } catch (e) { this.app.toast(e.message, "error"); }
    };
    dlg.showModal();
  }

  openImpact() {
    const dlg = document.getElementById("dlg-impact");
    const sym = dlg.querySelector(".im-symbol");
    sym.innerHTML = ["NIFTY 50", "NIFTY BANK", "SENSEX", "INDIA VIX"].filter((s) => this.app.symbols.some((x) => x.symbol === s))
      .map((s) => `<option>${s}</option>`).join("");
    const run = async () => {
      const kw = dlg.querySelector(".im-q").value.trim();
      if (kw.length < 2) return;
      dlg.querySelector(".im-out").innerHTML = `<div class="dim">Analysing…</div>`;
      try {
        const r = await req("GET", `/api/news/impact?${q({ q: kw, symbol: sym.value })}`);
        const S = r.summary;
        const row = (h) => S[h] ? `<tr><td>${h} min</td><td>${S[h].n}</td><td class="${S[h].avg >= 0 ? "up" : "down"}">${S[h].avg.toFixed(3)}%</td><td>${S[h].median.toFixed(3)}%</td>
            <td>${S[h].up_pct.toFixed(0)}%</td><td><b>${S[h].avg_abs.toFixed(3)}%</b></td><td>${S[h].normal_abs == null ? "—" : S[h].normal_abs.toFixed(3) + "%"}</td></tr>` : "";
        dlg.querySelector(".im-out").innerHTML = `<p class="small">${r.n_headlines} headlines contain “${esc(kw)}”; ${r.events.length} were during market hours with ${esc(r.symbol)} prices
          (headlines in the same 15 minutes counted once).</p>
          <table><tr><th>After</th><th>n</th><th>avg move</th><th>median</th><th>up</th><th>avg size |move|</th><th>normal size</th></tr>${["15", "30", "60"].map(row).join("")}</table>
          <p class="small dim">“Normal size” = average absolute move over the same horizon from every 15-minute mark of the same days. If the news size is not clearly bigger,
          the headlines did not move the market more than usual. GDELT times are when the article was seen (≈ 15-min steps), so part of the reaction can happen before.</p>
          <div class="table-wrap"><table><tr><th>Time</th><th>Headline</th><th>15m</th><th>30m</th><th>60m</th></tr>${r.events.slice(0, 300).map((e) =>
            `<tr><td>${fmtTime(e.time)}</td><td>${esc(e.title)}</td>${["15", "30", "60"].map((h) => `<td class="${(e["ret_" + h] ?? 0) >= 0 ? "up" : "down"}">${e["ret_" + h] == null ? "—" : e["ret_" + h].toFixed(2) + "%"}</td>`).join("")}</tr>`).join("")}</table></div>`;
      } catch (e) { dlg.querySelector(".im-out").innerHTML = `<div class="down">${esc(e.message)}</div>`; }
    };
    dlg.querySelector(".im-run").onclick = run;
    dlg.querySelector(".im-q").onkeydown = (e) => { if (e.key === "Enter") run(); };
    dlg.showModal();
  }
}
export { fmtPrice, TF_SECONDS };
