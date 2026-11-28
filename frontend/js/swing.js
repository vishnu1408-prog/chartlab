// Swing picks dashboard (paper trading). Polls /api/swing; browser notifications for new BUY / SELL / TARGET alerts.
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (v, d = 1) => (v == null || !isFinite(v) ? "–" : `${v >= 0 ? "+" : ""}${(100 * v).toFixed(d)}%`);
  const rs = (v) => (v == null || !isFinite(v) ? "–" : `₹${Math.round(v).toLocaleString("en-IN")}`);
  const num = (v) => (v == null || !isFinite(v) ? "–" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 }));
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  const SEEN = "chartlab.swing.seen";
  const ALERT_KINDS = new Set(["BUY", "SELL", "TARGET", "BOUGHT", "SOLD", "WATCH"]);
  let eqChart = null;

  function seen() {
    try { return new Set(JSON.parse(localStorage.getItem(SEEN) || "null") || []); } catch { return null; }
  }
  function saveSeen(set) {
    try { localStorage.setItem(SEEN, JSON.stringify([...set].slice(-500))); } catch { /* private window */ }
  }

  function notifyNew(events) {
    let s = seen();
    const first = s === null;
    s = s || new Set();
    const fresh = events.filter((e) => !s.has(e.id));
    fresh.forEach((e) => s.add(e.id));
    saveSeen(s);
    if (first || !fresh.length) return;                      // first visit: don't replay old alerts
    const imp = fresh.filter((e) => ALERT_KINDS.has(e.kind));
    if (!imp.length) return;
    document.title = `(${imp.length}) Swing picks`;
    if ("Notification" in window && Notification.permission === "granted") {
      imp.slice(-5).forEach((e) => new Notification(`${e.kind} ${e.sym}`, { body: e.text.slice(0, 240), tag: e.id }));
    }
  }

  function why(w) {
    if (!w) return "";
    const f = (w.for || []).map((a) => `<span class="f" title="${esc(a.rank)} of the 300 stocks">▲ ${esc(a.what)} ${esc(a.value)} · ${esc(a.rank)}</span>`);
    const a = (w.against || []).map((a) => `<span class="a">▼ ${esc(a.what)} ${esc(a.value)}</span>`);
    return `<div class="why">${f.join("")}${a.join("")}</div>`;
  }

  function renderTodo(d) {
    const day = d.latest?.day;
    const recent = d.events.filter((e) => e.day === day || (d.pending.length && e.kind === "BUY" && d.pending.some((p) => p.sym === e.sym)));
    const due = d.positions.filter((p) => p.exit_due);
    let h = "";
    if (d.pending.length) {
      h += d.pending.map((p) => p.filled_today ? `<div class="ev"><span class="k BUY">BOUGHT</span><span><b class="sym click" data-sym="${esc(p.sym)}">${esc(p.sym)}</b>
        paper-bought at today's open <b>${num(p.filled_today)}</b> (about ${p.fill_qty} shares). Stop ${num(p.filled_today * 0.9)} on a closing basis,
        target ${num(p.filled_today * 1.25)}. <span class="dim">Joins the table tonight, after NSE publishes today's prices (~18:00).</span></span></div>`
        : `<div class="ev"><span class="k BUY">BUY</span><span><b class="sym click" data-sym="${esc(p.sym)}">${esc(p.sym)}</b> at the next market open
        (last close ${num(p.close)}). Stop: sell if a day <i>closes</i> below ${num(p.close * 0.9)} (−10%). Optional target
        ${num(p.close * 1.25)} (+25%). Hold up to 30 trading days (~6 weeks).<br><span class="dim">Why: ${esc(p.reason)}</span></span></div>`).join("");
    }
    h += due.map((p) => p.filled_today ? `<div class="ev"><span class="k SELL">SOLD</span><span><b class="sym click" data-sym="${esc(p.sym)}">${esc(p.sym)}</b>
      paper-sold at today's open <b>${num(p.filled_today)}</b> — ${esc(p.exit_due)}: <span class="${cls(p.fill_ret)}">${pct(p.fill_ret)} · ${rs(p.fill_pnl)}</span> after costs
      (entry ${num(p.entry)}). <span class="dim">Moves to closed trades tonight (~18:00, official NSE open).</span></span></div>`
      : `<div class="ev"><span class="k SELL">SELL</span><span><b class="sym click" data-sym="${esc(p.sym)}">${esc(p.sym)}</b> at the next market open — ${esc(p.exit_due)}
      (entry ${num(p.entry)}, last ${num(p.last)}, ${pct(p.ret)})</span></div>`).join("");
    h += recent.filter((e) => e.kind === "TARGET").map((e) => `<div class="ev"><span class="k TARGET">TARGET</span><span>${esc(e.text)}</span></div>`).join("");
    if (!h) {
      const nxt = d.latest && !d.latest.official ? "Next picks: Friday evening, after NSE publishes the day's prices (~18:30–19:30)." : "";
      h = `<span class="dim">Nothing to do right now. ${nxt} Exits are checked every evening on the closing price.</span>`;
    }
    $("todo-body").innerHTML = h;
  }

  function renderPositions(d) {
    const P = d.positions;
    $("pos-note").innerHTML = `${P.length} of ${d.rules.slots} slots · ₹${(d.rules.capital / d.rules.slots).toLocaleString("en-IN")} each · open P&amp;L
      <b class="${cls(d.open_pnl)}">${rs(d.open_pnl)}</b>${d.live ? ' · <span class="pill paper">● LIVE prices</span>' : " · last NSE close"}`;
    if (!P.length) {
      const can = !d.closed.length && !d.pending.length;
      $("pos").innerHTML = `<tr><td class="dim">No open paper positions. ${can ? `<button class="b" id="btn-start">▶ Start paper trading now</button>
        — buys the current top ${d.rules.new_per_week} at the live price (market hours); after that 2 more every Friday.` : ""}</td></tr>`;
      const bs = $("btn-start");
      if (bs) bs.onclick = async () => {
        bs.disabled = true;
        const r = await (await fetch("/api/swing/start", { method: "POST" })).json();
        if (!r.started) $("asof").textContent = `Not started: ${r.why}`;
        load();
      };
      return;
    }
    $("pos").innerHTML = `<tr><th>Stock</th><th class="n">Qty</th><th class="n">Entry</th><th>Since</th><th class="n">Price</th>
      <th class="n">P&amp;L</th><th class="n">Stop (close, trails)</th><th class="n">Target (close)</th><th class="n">Day</th><th>Status</th></tr>` +
      P.map((p) => {
        const px = p.filled_today ?? p.ltp ?? p.last ?? p.entry, ret = px / p.entry - 1;
        const st = p.filled_today ? `<b class="down">SOLD at today's open ${num(p.filled_today)} — ${esc(p.exit_due)}</b>`
          : p.exit_due ? `<b class="down">sell at next open — ${esc(p.exit_due)}</b>`
          : p.below_stop_now ? `<b class="warn">below stop now — sell only if it CLOSES below ${num(p.stop)}</b>`
          : p.trailing ? `<span class="up">holding · stop trailing (profit locked)</span>` : "holding";
        return `<tr><td class="sym click" data-sym="${esc(p.sym)}">${esc(p.sym)}</td><td class="n">${p.qty}</td><td class="n">${num(p.entry)}</td>
        <td>${esc(p.entry_day)}</td><td class="n">${num(px)}${p.ltp != null ? ' <span class="up">●</span>' : ""}</td>
        <td class="n ${cls(ret)}">${pct(ret)} · ${rs(p.qty * (px - p.entry))}</td>
        <td class="n">${num(p.stop)}</td><td class="n">${num(p.target)}</td><td class="n">${p.days}/${d.rules.hold_days}</td><td>${st}</td></tr>`;
      }).join("");
  }

  function renderRank(d) {
    const L = d.latest;
    if (!L) { $("rank").innerHTML = `<tr><td class="dim">No ranking yet.</td></tr>`; return; }
    const pend = new Set(d.pending.map((p) => p.sym));
    const held = new Set(d.positions.map((p) => p.sym));
    $("rank-note").textContent = `${L.official ? "official Friday picks" : "preview"} from the ${L.day} close · the 300 most-traded NSE stocks · ` +
      `model trained to ${L.model?.trained_until} · market: ${Math.round(100 * L.market.m_above200)}% of stocks above their 200-day average`;
    $("rank").innerHTML = `<tr><th>#</th><th>Stock</th><th class="n">Close</th><th class="n">Stop −10%</th><th class="n">Target +25%</th>
      <th class="n">Qty</th><th class="n">1 mo</th><th class="n">6 mo</th><th>Why the model likes it</th></tr>` +
      L.top.map((r) => `<tr><td>${r.rank}</td><td class="sym click" data-sym="${esc(r.sym)}">${esc(r.sym)}${pend.has(r.sym) ? ' <span class="pill paper">BUY</span>' : held.has(r.sym) ? ' <span class="pill">held</span>' : ""}</td>
        <td class="n">${num(r.close)}</td><td class="n">${num(r.stop)}</td><td class="n">${num(r.target)}</td><td class="n">${r.qty}</td>
        <td class="n ${cls(r.r20)}">${pct(r.r20, 0)}</td><td class="n ${cls(r.r120)}">${pct(r.r120, 0)}</td><td>${why(r.why)}</td></tr>`).join("");
  }

  function renderEvents(d) {
    const E = [...d.events].reverse();
    $("events").innerHTML = E.length ? E.slice(0, 40).map((e) => `<div class="ev"><span class="k ${esc(e.kind)}">${esc(e.kind)}</span>
      <span><span class="dim">${esc(e.day)}</span> ${esc(e.text)}</span></div>`).join("") : `<span class="dim">No alerts yet.</span>`;
  }

  function renderTally(d) {
    const t = d.tally;
    $("live-note").textContent = d.live_from ? `since ${d.live_from}` : "";
    $("tally").innerHTML = `<div>Closed trades<b>${t.trades}</b></div><div>Win rate<b>${t.trades ? Math.round(100 * t.win) + "%" : "–"}</b></div>
      <div>Avg per trade<b class="${cls(t.avg)}">${t.trades ? pct(t.avg) : "–"}</b></div><div>Total P&amp;L<b class="${cls(t.pnl)}">${t.trades ? rs(t.pnl) : "–"}</b></div>`;
    const C = [...d.closed].reverse();
    $("closed").innerHTML = C.length ? `<tr><th>Stock</th><th>In</th><th>Out</th><th class="n">Entry</th><th class="n">Exit</th><th class="n">Return</th><th>Why out</th></tr>` +
      C.map((c) => `<tr><td class="sym click" data-sym="${esc(c.sym)}">${esc(c.sym)}</td><td>${esc(c.entry_day)}</td><td>${esc(c.exit_day)}</td><td class="n">${num(c.entry)}</td>
        <td class="n">${num(c.exit)}</td><td class="n ${cls(c.ret)}">${pct(c.ret)} · ${rs(c.pnl)}</td><td>${esc(c.exit_due)}${c.provisional ? ' <span class="dim">(today, provisional)</span>' : ""}</td></tr>`).join("") : "";
  }

  function renderBacktest(d) {
    const B = d.backtest;
    if (!B) return;
    $("verdict").textContent = B.verdict;
    $("periods").innerHTML = `<tr><th>Period</th><th class="n">Per year</th><th class="n">Worst fall</th><th class="n">Trades/mo</th><th class="n">Win</th><th class="n">Avg/trade</th></tr>` +
      Object.entries(B.periods).map(([k, s]) => `<tr><td>${esc(k)}</td><td class="n ${cls(s.cagr)}">${pct(s.cagr)}</td><td class="n down">${pct(s.maxdd, 0)}</td>
        <td class="n">${s.trades_mo.toFixed(1)}</td><td class="n">${Math.round(100 * s.win)}%</td><td class="n ${cls(s.avg)}">${pct(s.avg)}</td></tr>`).join("");
    $("years").innerHTML = "By year: " + Object.entries(B.by_year).map(([y, v]) => `${y} <span class="${cls(v)}">${pct(v, 0)}</span>`).join(" · ");
    $("bench").innerHTML = `<tr><th>Compare with</th><th class="n">2014–21</th><th class="n">2022–26</th></tr>` +
      B.benchmarks.map((b) => `<tr><td>${esc(b.name)}</td><td class="n">${pct(b.is)}</td><td class="n">${pct(b.oos)}</td></tr>`).join("");
    const r = d.rules;
    $("rules").innerHTML = `Every Friday after the close, the ${r.universe} most-traded NSE stocks are ranked by a machine-learning model on
      19 price and volume measures. The ${r.new_per_week} best-ranked stocks you don't already hold are bought at Monday's open (at most
      ${r.slots} stocks, ₹${(r.capital / r.slots).toLocaleString("en-IN")} each). Each is sold at the next open after ${r.hold_days} trading days,
      after a day closes ${Math.round(100 * r.stop)}% below the entry, or after a day closes ${Math.round(100 * r.target)}% above it (target —
      tested: about the same result as holding). Once a day closes 10% above the entry, the stop moves up to breakeven and then trails
      10% below the highest close (never down). Costs ${(200 * r.cost_side).toFixed(1)}% per round trip. Long only, delivery — no leverage, no F&amp;O.
      The reasons shown are the measures that raise the model's score the most for that stock (each compared with the median stock).`;
    if (!eqChart && window.LightweightCharts) {
      eqChart = LightweightCharts.createChart($("eq"), { autoSize: true, layout: { background: { color: "transparent" }, textColor: "#787b86" },
        grid: { vertLines: { visible: false }, horzLines: { color: "#2a2e39" } }, rightPriceScale: { mode: 1 }, timeScale: { borderVisible: false } });
      const s = eqChart.addSeries(LightweightCharts.LineSeries, { color: "#f5a623", lineWidth: 2, priceFormat: { type: "custom", formatter: (v) => `₹${(1.5 * v).toFixed(2)} L` } });
      s.setData(B.equity.map(([t, v]) => ({ time: t, value: v })));
      eqChart.timeScale().fitContent();
    }
  }

  async function load() {
    try {
      const r = await fetch("/api/swing");
      const d = await r.json();
      lastSnap = d;
      const st = d.status || {};
      $("asof").textContent = `data to ${st.last_day || d.latest?.day || "?"}${st.state === "updating" ? " · updating…" : ""}${st.error ? " · ⚠ " + st.error : ""}`;
      renderTodo(d); renderPositions(d); renderRank(d); renderEvents(d); renderTally(d); renderBacktest(d);
      notifyNew(d.events);
    } catch (e) {
      $("asof").textContent = "⚠ ChartLab not reachable";
    }
  }

  // ── stock chart pop-up ──
  let mChart = null, mSym = null, mView = "daily", mTimer = null, mData = null;

  function drawChart() {
    const d = mData;
    if (!d) return;
    if (mChart) { mChart.remove(); mChart = null; }
    const LW = window.LightweightCharts;
    mChart = LW.createChart($("mchart"), { autoSize: true, layout: { background: { color: "transparent" }, textColor: "#a3a6af" },
      grid: { vertLines: { color: "#2a2e3955" }, horzLines: { color: "#2a2e3955" } },
      timeScale: { borderVisible: false, timeVisible: mView === "intraday", secondsVisible: false },
      rightPriceScale: { borderVisible: false }, crosshair: { mode: 0 } });
    const cs = mChart.addSeries(LW.CandlestickSeries, { upColor: "#26a69a", downColor: "#ef5350", borderVisible: false,
      wickUpColor: "#26a69a", wickDownColor: "#ef5350" });
    let leg = "";
    if (mView === "daily") {
      cs.setData(d.daily);
      mChart.addSeries(LW.LineSeries, { color: "#2962ff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(d.ma50);
      mChart.addSeries(LW.LineSeries, { color: "#f5a623", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(d.ma200);
      leg = `Daily candles (adjusted for bonus / split), <b style="color:#2962ff">50-day</b> and <b style="color:#f5a623">200-day</b> averages. Last NSE close ${d.daily.at(-1)?.time}.`;
      mChart.timeScale().setVisibleLogicalRange({ from: Math.max(0, d.daily.length - 180), to: d.daily.length + 3 });
    } else {
      if (d.intraday.length) { cs.setData(d.intraday); mChart.timeScale().fitContent(); }
      leg = d.intraday.length ? `Today's 5-minute candles from Angel One (refresh every 30 s while this window is open).`
                              : `No intraday candles yet: market closed, Angel One not connected, or Angel is busy (it retries every 30 s). The daily chart still works.`;
    }
    const pos = d.position, pend = d.pending, rank = d.rank;
    const lv = pos ? [["Entry", pos.entry, "#d1d4dc"], ["Stop (close) −10%", pos.stop, "#ef5350"], ["Target (close) +25%", pos.target, "#26a69a"]]
      : pend ? [["Buy ≈ next open (last close)", pend.close, "#d1d4dc"], ["Stop −10%", pend.close * 0.9, "#ef5350"], ["Target +25%", pend.close * 1.25, "#26a69a"]]
      : rank ? [["Stop if bought at close −10%", rank.stop, "#ef535099"], ["Target +25%", rank.target, "#26a69a99"]] : [];
    lv.forEach(([t, p, c]) => cs.createPriceLine({ price: p, color: c, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: t }));
    $("m-legend").innerHTML = leg;
  }

  function renderInfo() {
    const d = mData, r = d.rank, p = d.position;
    let h = "";
    if (p) h += `<p><b>Paper position:</b> ${p.qty} shares at ${num(p.entry)} since ${esc(p.entry_day)} · day ${p.days}/30 ·
      now ${num(p.ltp ?? p.last ?? p.entry)} (<span class="${cls((p.ltp ?? p.last ?? p.entry) / p.entry - 1)}">${pct((p.ltp ?? p.last ?? p.entry) / p.entry - 1)}</span>) ${p.exit_due ? `· <b class="down">sell at next open — ${esc(p.exit_due)}</b>` : ""}</p>`;
    if (d.pending) h += `<p><b class="up">BUY at the next open</b> (paper) — stop and target are set from the actual fill price.</p>`;
    if (r) h += `<p><b>Rank #${r.rank}</b> of 300 on ${esc(mDataDay())}. ${why(r.why)}</p>`;
    else h += `<p class="dim">Not in the current top 15.</p>`;
    if (d.closed.length) h += `<p class="dim">Earlier paper trades: ${d.closed.map((c) => `${esc(c.entry_day)} → ${esc(c.exit_day)} ${pct(c.ret)}`).join(" · ")}</p>`;
    $("m-info").innerHTML = h;
  }
  let lastSnap = null;
  const mDataDay = () => lastSnap?.latest?.day || "";

  async function fetchChart() {
    const r = await fetch(`/api/swing/chart?sym=${encodeURIComponent(mSym)}`);
    if (!r.ok) { $("m-legend").textContent = `No data for ${mSym}`; return; }
    mData = await r.json();
    const d = mData;
    $("m-ltp").innerHTML = d.live ? `LTP <b>${num(d.ltp)}</b> <span class="${cls(d.change_pct)}">${d.change_pct >= 0 ? "+" : ""}${(d.change_pct ?? 0).toFixed(2)}%</span>`
                                  : `close ${num(d.daily.at(-1)?.close)} <span class="dim">(no live feed)</span>`;
    drawChart(); renderInfo();
  }

  async function openChart(sym) {
    mSym = sym; mView = "daily";
    document.querySelectorAll(".seg2 button").forEach((b) => b.classList.toggle("on", b.dataset.v === "daily"));
    $("m-sym").textContent = sym;
    $("m-tv").href = `https://www.tradingview.com/chart/?symbol=NSE:${encodeURIComponent(sym)}`;
    $("modal").classList.add("on");
    await fetchChart();
    clearInterval(mTimer);
    mTimer = setInterval(fetchChart, 30000);
  }
  function closeChart() { $("modal").classList.remove("on"); clearInterval(mTimer); if (mChart) { mChart.remove(); mChart = null; } }
  document.addEventListener("click", (e) => {
    const t = e.target.closest(".sym.click");
    if (t) openChart(t.dataset.sym);
  });
  $("m-close").onclick = closeChart;
  $("modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeChart(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeChart(); });
  document.querySelectorAll(".seg2 button").forEach((b) => b.onclick = () => {
    mView = b.dataset.v;
    document.querySelectorAll(".seg2 button").forEach((x) => x.classList.toggle("on", x === b));
    drawChart();
  });

  $("btn-refresh").onclick = async () => { await fetch("/api/swing/update", { method: "POST" }); $("asof").textContent = "updating…"; setTimeout(load, 8000); };
  const nb = $("btn-notify");
  const setNb = () => { nb.textContent = !("Notification" in window) ? "🔕 No browser alerts" : Notification.permission === "granted" ? "🔔 Alerts on" : "🔔 Enable alerts"; };
  nb.onclick = async () => { if ("Notification" in window) { await Notification.requestPermission(); setNb(); } };
  setNb();
  window.addEventListener("focus", () => { document.title = "Swing picks"; });
  load();
  // every 30 s during NSE hours (live prices), every 2 min otherwise
  (function tick() {
    const t = new Date(Date.now() + (330 + new Date().getTimezoneOffset()) * 60000);   // IST
    const m = t.getHours() * 60 + t.getMinutes(), open = t.getDay() > 0 && t.getDay() < 6 && m >= 555 && m <= 935;
    setTimeout(() => { load(); tick(); }, open ? 30000 : 120000);
  })();
})();
