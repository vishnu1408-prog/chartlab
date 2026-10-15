// Alerts: price crossing a drawn line (horizontal / trend / ray with 🔔 on), or a new signal from an indicator with 🔔
// on. Fires a toast + beep + desktop notification, and (optionally) pauses the replay so you can decide.
import { store } from "./api.js";
import { fmtPrice, fmtTime } from "./core.js";

/** price of a line drawing at time t (null if the line does not exist at t) — pure, tested in Node */
export function lineLevel(d, t) {
  const [a, b] = d.points;
  if (d.type === "hline") return a.price;
  if (!b || b.time === a.time) return null;
  const k = (b.price - a.price) / (b.time - a.time);
  if (d.type === "trend" && (t < Math.min(a.time, b.time) || t > Math.max(a.time, b.time))) return null;
  if (d.type === "ray" && (b.time >= a.time ? t < a.time : t > a.time)) return null;
  return a.price + k * (t - a.time);
}

/** did the bar cross the level coming from `prevClose`? */
export function crossed(level, prevClose, bar) {
  if (level == null || prevClose == null) return false;
  return (prevClose < level && bar.high >= level) || (prevClose > level && bar.low <= level);
}

export class AlertManager {
  constructor(app) {
    this.app = app;
    this.pauseReplay = store.get("alertPause", true);
    this.sound = store.get("alertSound", true);
  }

  arm() {
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().catch(() => {});
  }

  /** new price data in a window: check its line alerts */
  checkLines(panel, prevClose, bar) {
    for (const d of panel.drawings.items) {
      if (!d.alert || !["hline", "trend", "ray"].includes(d.type)) continue;
      const lv = lineLevel(d, bar.time);
      if (crossed(lv, prevClose, bar)) this.fire(`${panel.state.symbol} crossed your ${d.type === "hline" ? "line" : d.type} at ${fmtPrice(lv)}`, panel);
    }
  }

  indicator(panel, a, name, m) {
    if (!a.alert) return;
    this.fire(`${panel.state.symbol}: ${name} — ${m.text || (m.shape === "arrowUp" ? "BUY" : m.shape === "arrowDown" ? "SELL" : "signal")} (${this.app.replay.blind ? "" : fmtTime(m.time)})`, panel);
  }

  fire(msg, panel) {
    this.app.toast(`🔔 ${msg}`, "ok");
    if (this.sound) beep();
    if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
      try { new Notification("ChartLab alert", { body: msg }); } catch { /* not allowed here */ }
    }
    if (this.pauseReplay && this.app.replay.active) this.app.replay.pause();
    if (panel) { panel.el.classList.add("flash"); setTimeout(() => panel.el.classList.remove("flash"), 1200); }
  }
}

let actx = null;
function beep() {
  try {
    actx ||= new (window.AudioContext || window.webkitAudioContext)();
    const o = actx.createOscillator(), g = actx.createGain();
    o.frequency.value = 880; g.gain.value = 0.08;
    o.connect(g); g.connect(actx.destination);
    o.start(); o.stop(actx.currentTime + 0.18);
  } catch { /* audio not available */ }
}
