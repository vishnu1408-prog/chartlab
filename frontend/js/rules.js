// Discipline guard: your own trading rules, enforced on every practice order (replay and live).
// Pure checks live in `checkOrder` / `dayStats` so they can be unit-tested without a browser.
import { store } from "./api.js";
import { dayOf, minuteOfDay, fmtPrice } from "./core.js";

export const DEFAULT_RULES = {
  enabled: true,
  maxTrades: 2,            // entries per day
  dailyLoss: 1500,         // ₹ — stop trading for the day once realised + open loss reaches this
  noEntryAfter: "14:30",
  noEntryBefore: "09:20",
  requireSL: true,
  maxQty: 130,             // units (e.g. 2 lots of 65)
  cooldownMin: 15,         // minutes to wait after a losing trade
  maxConsecLosses: 2,      // stop for the day after this many losses in a row
};

const hm = (s) => { const [h, m] = String(s).split(":").map(Number); return h * 60 + (m || 0); };

/** Statistics of one trading day from the engine's closed trades (+ open position). */
export function dayStats(trades, pos, time, unrealized = 0) {
  const d = dayOf(time);
  const today = trades.filter((t) => dayOf(t.exit_time) === d || dayOf(t.entry_time) === d);
  const realised = today.reduce((a, t) => a + t.pnl_rs, 0);
  let consec = 0;
  for (let i = today.length - 1; i >= 0 && today[i].pnl_rs < 0; i--) consec++;
  const entries = new Set(today.map((t) => t.entry_time)).size + (pos && dayOf(pos.time) === d && !today.some((t) => t.entry_time === pos.time) ? 1 : 0);
  const lastLoss = [...today].reverse().find((t) => t.pnl_rs < 0);
  return { entries, realised, pnl: realised + unrealized, consec, lastLossExit: lastLoss ? lastLoss.exit_time : null };
}

/** Returns null if the order is allowed, else the rule it breaks (plain text). */
export function checkOrder(rules, { time, qty, sl, isNewEntry, trades, pos, unrealized = 0 }) {
  if (!rules.enabled || !isNewEntry) return null;
  const m = minuteOfDay(time);
  const s = dayStats(trades, pos, time, unrealized);
  if (rules.noEntryBefore && m < hm(rules.noEntryBefore)) return `no entries before ${rules.noEntryBefore}`;
  if (rules.noEntryAfter && m > hm(rules.noEntryAfter)) return `no new entries after ${rules.noEntryAfter}`;
  if (rules.maxTrades && s.entries >= rules.maxTrades) return `max ${rules.maxTrades} trades a day reached`;
  if (rules.dailyLoss && s.pnl <= -rules.dailyLoss) return `daily loss limit ₹${fmtPrice(rules.dailyLoss, 0)} hit — done for today`;
  if (rules.maxConsecLosses && s.consec >= rules.maxConsecLosses) return `${s.consec} losses in a row — done for today`;
  if (rules.cooldownMin && s.lastLossExit != null && time - s.lastLossExit < rules.cooldownMin * 60)
    return `cool-down: wait ${Math.ceil((rules.cooldownMin * 60 - (time - s.lastLossExit)) / 60)} min after a loss`;
  if (rules.requireSL && !(sl > 0)) return "a stop loss is required";
  if (rules.maxQty && qty > rules.maxQty) return `quantity above your max (${rules.maxQty})`;
  return null;
}

export class Rules {
  constructor(app) {
    this.app = app;
    this.rules = { ...DEFAULT_RULES, ...store.get("rules", {}) };
    this.breaks = store.get("ruleBreaks", {});       // rule text → times you tried to break it
  }
  save() { store.set("rules", this.rules); }
  noteBreak(msg) {
    const k = msg.startsWith("cool-down") ? "cool-down after a loss" : msg.replace(/[0-9₹,.:()]+/g, "").replace(/\s+/g, " ").replace(/ —.*/, "").trim();
    this.breaks[k] = (this.breaks[k] || 0) + 1;
    store.set("ruleBreaks", this.breaks);
  }

  open() {
    const dlg = document.getElementById("dlg-rules");
    const r = this.rules;
    const f = dlg.querySelector(".form");
    const row = (k, label, type, extra = "") =>
      `<label class="form-row"><span>${label}</span>${type === "bool" ? `<input type="checkbox" name="${k}" ${r[k] ? "checked" : ""}>`
        : `<input type="${type}" name="${k}" value="${r[k]}" ${extra}>`}</label>`;
    f.innerHTML = row("enabled", "Rules on", "bool") + row("maxTrades", "Max trades per day", "number", 'min="0"')
      + row("dailyLoss", "Daily loss limit ₹ (auto-close + stop)", "number", 'min="0" step="100"') + row("noEntryBefore", "No entries before", "time")
      + row("noEntryAfter", "No new entries after", "time") + row("requireSL", "Stop loss required", "bool")
      + row("maxQty", "Max quantity (units)", "number", 'min="0"') + row("cooldownMin", "Cool-down after a loss (min)", "number", 'min="0"')
      + row("maxConsecLosses", "Stop after N losses in a row", "number", 'min="0"')
      + `<div class="dim small">0 = rule off. Broken-rule attempts so far: ${Object.entries(this.breaks).map(([k, v]) => `${k} ×${v}`).join(" · ") || "none"}</div>`;
    dlg.querySelector(".apply").onclick = () => {
      for (const el of f.querySelectorAll("input")) r[el.name] = el.type === "checkbox" ? el.checked : el.type === "number" ? Number(el.value) : el.value;
      this.save(); dlg.close(); this.app.practice.render();
    };
    dlg.querySelector(".defaults").onclick = () => { this.rules = { ...DEFAULT_RULES }; this.save(); dlg.close(); this.open(); };
    dlg.showModal();
  }
}
