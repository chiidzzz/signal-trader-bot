const evBox = document.getElementById("events");
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const cfgForm = document.getElementById("cfgForm");
const saveInfo = document.getElementById("saveInfo");

let es;
let lastBackend = Date.now();

// -----------------------------------------------------------------------------
// Live Events (SSE)
// -----------------------------------------------------------------------------
function connectSSE() {
  if (es) es.close();
  es = new EventSource("/events");

  es.onopen = () => {
    statusDot.className = "dot green";
    statusDot.title = "Connected";
  };

  es.onerror = () => {
    statusDot.className = "dot gray";
    statusDot.title = "Reconnecting…";
    setTimeout(connectSSE, 2000);
  };

  es.onmessage = (e) => {
    try {
      const obj = JSON.parse(e.data);

      // Handle backend status messages (update status bar ONLY, don't log)
      if (obj.type === "status_text") {
        statusText.textContent = obj.msg;
        const isDown =
          obj.msg.includes("DOWN") ||
          obj.msg.includes("🚨") ||
          obj.msg.includes("⚠️");
        statusText.style.color = isDown ? "gold" : "limegreen";
        statusDot.className = isDown ? "dot orange" : "dot green";
        lastBackend = Date.now();
        return; // ✅ DON'T render status updates in event log
      }

      // Normal log events - render these
      renderEvent(obj);
    } catch {
      // ignore malformed lines
    }
  };
}

// -----------------------------------------------------------------------------
// Event rendering
// -----------------------------------------------------------------------------
function renderEvent(ev) {
  const d = new Date(ev.ts * 1000).toLocaleTimeString();
  const el = document.createElement("div");
  el.className = "event";
  el.textContent = `[${d}] ${ev.msg || ev.type} `;
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
        el.checked = !!val;
      } else if (val !== undefined && val !== null) {
        el.value = val;
      }
    }

    // Core settings
    set("dry_run", cfg.dry_run);
    set("quote_asset", cfg.quote_asset);
    set("capital_entry_pct_default", cfg.capital_entry_pct_default);
    set("override_capital_enabled", cfg.override_capital_enabled);
    set("max_slippage_pct", cfg.max_slippage_pct);
    set("use_limit_if_slippage_exceeds", cfg.use_limit_if_slippage_exceeds);
    set("respect_spot_only", cfg.respect_spot_only);
    set("min_notional_usdt", cfg.min_notional_usdt);
    set("limit_time_in_force_sec", cfg.limit_time_in_force_sec);
    set("prefer_symbol_in_parentheses", cfg.prefer_symbol_in_parentheses);
    set("fallback_to_name_search", cfg.fallback_to_name_search);
    set("override_tp_enabled", cfg.override_tp_enabled);
    set("override_tp_pct", cfg.override_tp_pct);
    set("override_sl_enabled", cfg.override_sl_enabled);
    set("override_sl_pct", cfg.override_sl_pct);
    set("override_sl_as_absolute", cfg.override_sl_as_absolute);
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
  const fields = [
    "dry_run",
    "quote_asset",
    "capital_entry_pct_default",
    "override_capital_enabled",
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
    const el = cfgForm.querySelector(`[name="${name}"]`);
    if (!el) continue;
    let val =
      el.type === "checkbox"
        ? el.checked
        : el.type === "number"
        ? Number(el.value)
        : el.value;
    data[name] = val;
  }

  saveInfo.textContent = "Saving…";
  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    if (res.ok) {
      saveInfo.textContent = "Saved ✔️";
      setTimeout(() => (saveInfo.textContent = ""), 2000);
    } else {
      saveInfo.textContent = "Failed ❌";
    }
  } catch (err) {
    console.error(err);
    saveInfo.textContent = "Failed ❌";
  }
};

// -----------------------------------------------------------------------------
// Init
// -----------------------------------------------------------------------------
connectSSE();

// -----------------------------------------------------------------------------
// Frontend heartbeat file (lets backend know UI is alive)
// -----------------------------------------------------------------------------
setInterval(async () => {
  try {
    await fetch("/api/ping", { method: "POST" });
  } catch {
    // ignore errors silently
  }
}, 5000); // every 5s (more frequent = more reliable detection)

loadConfig();
