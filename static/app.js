const evBox = document.getElementById("events");
const statusDot = document.getElementById("status-dot");
const cfgForm = document.getElementById("cfgForm");
const saveInfo = document.getElementById("saveInfo");

let es;

// -----------------------------------------------------------------------------
// Live Events (SSE)
// -----------------------------------------------------------------------------
function connectSSE() {
  if (es) es.close();
  es = new EventSource("/events");

  es.onopen = () => {
    statusDot.className = "dot green";
    statusDot.title = "connected";
  };
  es.onerror = () => {
    statusDot.className = "dot gray";
    statusDot.title = "reconnecting…";
    // retry after short backoff
    setTimeout(connectSSE, 2000);
  };
  es.onmessage = (e) => {
    try {
      const obj = JSON.parse(e.data);
      renderEvent(obj);
    } catch {
      // ignore non-JSON lines (e.g., comments/keepalive)
    }
  };
}

function renderEvent(ev) {
  const d = new Date(ev.ts * 1000).toLocaleTimeString();
  const el = document.createElement("div");
  el.className = "event";
  el.textContent = `[${d}] ${ev.type} :: ${JSON.stringify(ev)}`;
  evBox.prepend(el);
  while (evBox.childNodes.length > 500) evBox.removeChild(evBox.lastChild);
}

// -----------------------------------------------------------------------------
// Config Handling
// -----------------------------------------------------------------------------
async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    const cfg = await res.json();

    function set(name, val) {
      const el = cfgForm.querySelector(`[name="${name}"]`);
      if (!el) return;
      if (el.type === "checkbox") {
        if (typeof val === "boolean") el.checked = val; // only set when boolean provided
      } else {
        if (val !== undefined && val !== null) el.value = val;
      }
    }

    // Core options
    set("dry_run", cfg.dry_run);
    set("quote_asset", cfg.quote_asset);
    set("capital_entry_pct_default", cfg.capital_entry_pct_default);
    set("max_slippage_pct", cfg.max_slippage_pct);
    set("use_limit_if_slippage_exceeds", cfg.use_limit_if_slippage_exceeds);

    // Guards
    set("respect_spot_only", cfg.respect_spot_only);
    set("min_notional_usdt", cfg.min_notional_usdt);
    set("limit_time_in_force_sec", cfg.limit_time_in_force_sec);

    // Symbol resolution
    set("prefer_symbol_in_parentheses", cfg.prefer_symbol_in_parentheses);
    set("fallback_to_name_search", cfg.fallback_to_name_search);

    // TP/SL Overrides
    set("override_tp_enabled", cfg.override_tp_enabled);
    set("override_tp_pct", cfg.override_tp_pct);
    set("override_sl_enabled", cfg.override_sl_enabled);
    set("override_sl_pct", cfg.override_sl_pct);
    set("override_sl_as_absolute", cfg.override_sl_as_absolute);

    // Safety Watchdogs - FIXED: now properly loading these values
    set("flatten_check_interval_min", cfg.flatten_check_interval_min);
    set("heartbeat_max_idle_min", cfg.heartbeat_max_idle_min);
  } catch (err) {
    console.error("Failed to load config:", err);
  }
}

// -----------------------------------------------------------------------------
// Save Config
// -----------------------------------------------------------------------------
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
    "respect_spot_only",
    "min_notional_usdt",
    "limit_time_in_force_sec",
    "prefer_symbol_in_parentheses",
    "fallback_to_name_search",
    "override_tp_enabled",
    "override_tp_pct",
    "override_sl_enabled",
    "override_sl_pct",
    "override_sl_as_absolute",
    "flatten_check_interval_min",
    "heartbeat_max_idle_min",
  ];

  for (const name of fields) {
    const el = get(name);
    if (!el) continue;
    let val;
    if (el.type === "checkbox") {
      val = el.checked;
    } else if (el.type === "number") {
      // FIXED: Properly handle number inputs, including when they're empty
      const num = Number(el.value);
      val = isNaN(num) ? null : num;
    } else {
      val = el.value;
    }
    setNested(name, val);
  }

  saveInfo.textContent = "Saving…";
  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });

    if (res.ok) {
      saveInfo.textContent = "Saved ✔️ (bot reloads shortly)";
      setTimeout(() => (saveInfo.textContent = ""), 3000);
    } else {
      saveInfo.textContent = "Failed to save ❌";
    }
  } catch (err) {
    console.error("Save error:", err);
    saveInfo.textContent = "Failed to save ❌";
  }
};

// -----------------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------------
connectSSE();
loadConfig();
