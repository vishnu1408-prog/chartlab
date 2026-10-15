// Thin wrapper around the ChartLab REST API.
async function req(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || `${r.status} ${r.statusText}`);
  return data;
}

const q = (o) => Object.entries(o).filter(([, v]) => v !== undefined && v !== null).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");

export const api = {
  health: (symbol) => req("GET", "/api/health" + (symbol ? "?symbol=" + encodeURIComponent(symbol) : "")),
  symbols: () => req("GET", "/api/symbols"),
  coverage: (symbol) => req("GET", `/api/coverage?${q({ symbol })}`),
  sync: (symbol) => req("POST", `/api/sync?${q({ symbol, force: true })}`),
  candles: (o) => req("GET", `/api/candles?${q(o)}`),
  option: (o) => req("GET", `/api/option?${q(o)}`),
  indicators: () => req("GET", "/api/indicators"),
  compute: (id, body) => req("POST", `/api/indicators/${encodeURIComponent(id)}/compute`, body),
  drawings: (symbol) => req("GET", `/api/drawings?${q({ symbol })}`),
  saveDrawings: (symbol, items) => req("PUT", `/api/drawings?${q({ symbol })}`, items),
  journal: () => req("GET", "/api/journal"),
  addJournal: (t) => req("POST", "/api/journal", t),
  clearJournal: () => req("DELETE", "/api/journal"),
};

export const store = {
  get(k, d) { try { const v = localStorage.getItem("chartlab." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("chartlab." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};
