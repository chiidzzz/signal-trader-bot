const evBox = document.getElementById("events");
const stateBox = document.getElementById("stateBox");
const statusDot = document.getElementById("status-dot");
const cfgForm = document.getElementById("cfgForm");
const saveInfo = document.getElementById("saveInfo");
document.getElementById("refreshState").onclick = loadState;

let es;
function connectSSE() {
  if (es) es.close();
  es = new EventSource("/events");
  es.onopen = () => {
    statusDot.className = "dot green";
    statusDot.title = "live";
  };
  es.onerror = () => {
    statusDot.className = "dot gray";
    statusDot.title = "disconnected";
  };
  es.onmessage = (e) => {
    try {
      const obj = JSON.parse(e.data);
      renderEvent(obj);
    } catch (err) {}
  };
}
function renderEvent(ev) {
  const d = new Date(ev.ts * 1000).toLocaleTimeString();
  const el = document.createElement("div");
  el.className = "event";
  el.textContent = `[${d}] ${ev.type} :: ${JSON.stringify(ev)}`;
  evBox.prepend(el);
  // keep last 500
  while (evBox.childNodes.length > 500) evBox.removeChild(evBox.lastChild);
}

async function loadState() {
  const res = await fetch("/api/state");
  const data = await res.json();
  stateBox.textContent = JSON.stringify(data, null, 2);
}

async function loadConfig() {
  const res = await fetch("/api/config");
  const cfg = await res.json();
  // fill form
  const set = (name, val) => {
    const el = cfgForm.querySelector(`[name="${name}"]`);
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!val;
    else el.value = val ?? "";
  };
  set("dry_run", cfg.dry_run);
  set("quote_asset", cfg.quote_asset);
  set("capital_entry_pct_default", cfg.capital_entry_pct_default);
  set("max_slippage_pct", cfg.max_slippage_pct);
  set("use_limit_if_slippage_exceeds", cfg.use_limit_if_slippage_exceeds);

  set("tp_splits.tp1", cfg.tp_splits?.tp1);
  set("tp_splits.tp2", cfg.tp_splits?.tp2);
  set("tp_splits.runner", cfg.tp_splits?.runner);

  set("stop_loss_move_to_be_after_tp2", cfg.stop_loss_move_to_be_after_tp2);
  set("trailing_runner_enabled", cfg.trailing_runner_enabled);
  set("trailing_pct", cfg.trailing_pct);
  set("trailing_poll_sec", cfg.trailing_poll_sec);

  set("respect_spot_only", cfg.respect_spot_only);
  set("min_notional_usdt", cfg.min_notional_usdt);
  set("limit_time_in_force_sec", cfg.limit_time_in_force_sec);

  set("prefer_symbol_in_parentheses", cfg.prefer_symbol_in_parentheses);
  set("fallback_to_name_search", cfg.fallback_to_name_search);
}

cfgForm.onsubmit = async (e) => {
  e.preventDefault();
  const data = {};
  const get = (n) => cfgForm.querySelector(`[name="${n}"]`);
  const setNested = (path, val) => {
    const [a, b] = path.split(".");
    if (!b) {
      data[a] = val;
      return;
    }
    data[a] = data[a] || {};
    data[a][b] = val;
  };
  const fields = [
    "dry_run",
    "quote_asset",
    "capital_entry_pct_default",
    "max_slippage_pct",
    "use_limit_if_slippage_exceeds",
    "tp_splits.tp1",
    "tp_splits.tp2",
    "tp_splits.runner",
    "stop_loss_move_to_be_after_tp2",
    "trailing_runner_enabled",
    "trailing_pct",
    "trailing_poll_sec",
    "respect_spot_only",
    "min_notional_usdt",
    "limit_time_in_force_sec",
    "prefer_symbol_in_parentheses",
    "fallback_to_name_search",
  ];
  for (const name of fields) {
    const el = get(name);
    if (!el) continue;
    let val;
    if (el.type === "checkbox") val = el.checked;
    else if (el.type === "number") val = Number(el.value);
    else val = el.value;
    setNested(name, val);
  }
  saveInfo.textContent = "Saving…";
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (res.ok) {
    saveInfo.textContent = "Saved ✔️ (bot hot-reloads within seconds)";
    setTimeout(() => (saveInfo.textContent = ""), 3000);
  } else {
    saveInfo.textContent = "Failed to save ❌";
  }
};

connectSSE();
loadState();
loadConfig();
