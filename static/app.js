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

  // Build readable message based on event type
  let message = `[${d}] `;

  switch (ev.type) {
    case "new_message":
      message += `📨 New message: ${(ev.preview || "").substring(0, 80)}`;
      el.style.color = "#4dabf7";
      break;
    case "ignored":
      const reason = ev.reason || "unknown";
      const preview = (ev.preview || ev.msg || "").substring(0, 60);
      message += `❌ Ignored (${reason})${preview ? ": " + preview : ""}`;
      el.style.color = "#868e96";
      break;
    case "parse_success":
    case "ai_parse_success":
      message += `✅ Parsed: ${ev.currency} @ ${ev.entry}`;
      el.style.color = "#51cf66";
      break;
    case "signal_parsed":
      message += `✅ Signal parsed: ${ev.currency || ""} @ ${ev.entry || ""}`;
      el.style.color = "#51cf66";
      break;
    case "trade_executed":
      message += `💰 Trade: ${ev.msg || JSON.stringify(ev)}`;
      el.style.color = "#ffd43b";
      break;
    case "error":
      message += `🚨 Error: ${ev.msg || JSON.stringify(ev)}`;
      el.style.color = "#ff6b6b";
      break;
    case "parse_debug":
      // Skip debug events (too verbose), or uncomment to show them
      return;
      // message += `🔍 Debug [${ev.stage}]: ${ev.currency || ev.preview || ''}`;
      // el.style.color = '#495057';
      break;
    default:
      message += `${ev.type}: ${ev.msg || ev.preview || JSON.stringify(ev)}`;
  }

  el.textContent = message;
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
    set("machine_name", cfg.machine_name);
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

// Fetch and display Telegram configuration
async function loadTelegramConfig() {
  try {
    const res = await fetch("/api/telegram-config");
    const data = await res.json();

    // Source (Listening)
    const sourceNameEl = document.getElementById("tg-source-name");
    const sourceIdEl = document.getElementById("tg-source-id");

    if (sourceNameEl && data.source) {
      sourceNameEl.textContent = data.source.name || "Unknown";
    }
    if (sourceIdEl && data.source) {
      sourceIdEl.textContent = `ID: ${data.source.id}`;
    }

    // Destination (Sending)
    const destNameEl = document.getElementById("tg-dest-name");
    const destIdEl = document.getElementById("tg-dest-id");

    if (destNameEl && data.destination) {
      destNameEl.textContent = data.destination.name || "Unknown";
    }
    if (destIdEl && data.destination) {
      destIdEl.textContent = `ID: ${data.destination.id}`;
    }
  } catch (err) {
    console.error("Failed to load Telegram config:", err);
    const sourceNameEl = document.getElementById("tg-source-name");
    const destNameEl = document.getElementById("tg-dest-name");
    if (sourceNameEl) sourceNameEl.textContent = "Error loading";
    if (destNameEl) destNameEl.textContent = "Error loading";
  }
}

// -----------------------------------------------------------------------------
// Save Config
// -----------------------------------------------------------------------------
cfgForm.onsubmit = async (e) => {
  e.preventDefault();
  const data = {};
  const fields = [
    "machine_name",
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
loadTelegramConfig();
