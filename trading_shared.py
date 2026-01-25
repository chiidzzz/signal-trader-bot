# trading_shared.py
import os, json, time, yaml, csv, datetime, aiofiles
from typing import Optional, Dict
from pydantic import BaseModel, Field
from dataclasses import dataclass

# --- Constants ---
RUNTIME_DIR = "runtime"
EVENTS_FILE = os.path.join(RUNTIME_DIR, "events.jsonl")
STATE_FILE = os.path.join(RUNTIME_DIR, "state.json")
CONFIG_FILE = "config.yaml"
ALIASES_FILE = "token_aliases.json"
OCO_TRACKER = os.path.join(RUNTIME_DIR, "oco_tracker.json")

os.makedirs(RUNTIME_DIR, exist_ok=True)

# --- Models ---
class TPSet(BaseModel):
    tp1: float
    tp2: Optional[float] = None
    tp3: Optional[float] = None

@dataclass
class ParsedSignal:
    raw_text: str
    spot_only: bool
    currency_display: str
    symbol_hint: Optional[str]
    entry: float
    stop: Optional[float]
    tps: TPSet
    capital_pct: Optional[float]
    period_hours: Optional[int]

class Settings(BaseModel):
    dry_run: bool = False
    use_testnet: bool = False
    quote_asset: str = "USDT"
    capital_entry_pct_default: float = 0.80
    override_capital_enabled: bool = False
    max_slippage_pct: float = 0.015
    use_limit_if_slippage_exceeds: bool = True
    tp_splits: Dict[str, float] = Field(default_factory=lambda: {"tp1": 0.5, "tp2": 0.3, "runner": 0.2})
    stop_loss_move_to_be_after_tp2: bool = True
    trailing_runner_enabled: bool = True
    trailing_pct: float = 0.08
    trailing_poll_sec: int = 5
    respect_spot_only: bool = True
    min_notional_usdt: float = 5
    limit_time_in_force_sec: int = 180
    prefer_symbol_in_parentheses: bool = True
    fallback_to_name_search: bool = True
    override_tp_enabled: bool = False
    override_tp_pct: float = 0.03
    override_sl_enabled: bool = False
    override_sl_pct: float = 0.01
    override_sl_as_absolute: bool = False
    default_sl_pct: float = 0.1
    exit_mode: str = "fixed_oco"  # fixed_oco | trailing_tp
    trailing_tp_activation_pct: float = 0.01
    trailing_tp_pullback_pct: float = 0.005
    market_cap_filter_enabled: bool = False
    market_cap_min: float = 0
    market_cap_max: float = 0

# --- State & Config Helpers ---
def read_settings() -> Settings:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return Settings(**yaml.safe_load(f))
    except Exception:
        # Return defaults if file missing or broken
        return Settings()

def read_settings_dict() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

# Initialize Global Settings
SETTINGS = read_settings()
_last_cfg_mtime = 0
if os.path.exists(CONFIG_FILE):
    _last_cfg_mtime = os.path.getmtime(CONFIG_FILE)

def emit(event_type: str, payload: dict):
    """Append a compact JSON line for dashboard."""
    line = {"ts": int(time.time()), "type": event_type, **payload}
    try:
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"Error emitting event: {e}")

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def read_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_aliases() -> dict:
    try:
        with open(ALIASES_FILE, "r", encoding="utf-8") as f:
            aliases = json.load(f)
        return {k.strip().upper(): v.upper() for k, v in aliases.items()}
    except Exception as e:
        emit("warning", {"msg": f"Failed to load aliases: {e}"})
        return {}

TOKEN_ALIASES = load_aliases()

def maybe_reload_settings():
    global SETTINGS, _last_cfg_mtime
    try:
        if not os.path.exists(CONFIG_FILE):
            return
        mtime = os.path.getmtime(CONFIG_FILE)
        if mtime != _last_cfg_mtime:
            SETTINGS = read_settings()
            _last_cfg_mtime = mtime
            emit("config_reloaded", {"msg": "config.yaml reloaded"})
    except Exception as e:
        emit("warning", {"msg": f"Failed to reload config: {e}"})

async def log_error(msg: str):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs("runtime", exist_ok=True)
    async with aiofiles.open("runtime/errors.log", "a", encoding="utf-8") as f:
        await f.write(f"[{ts}] {msg}\n")

def log_trade_pnl(symbol, side, entry, exit, qty, pnl_usd, status):
    os.makedirs("runtime", exist_ok=True)
    path = "runtime/pnl_log.csv"
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["timestamp","symbol","side","entry","exit","qty","pnl_usd","status"])
        w.writerow([
            datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            symbol, side, entry, exit, qty, round(pnl_usd,4), status
        ])

# --- OCO Tracker Helpers ---
def _read_oco_tracker() -> dict:
    try:
        with open(OCO_TRACKER, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _write_oco_tracker(d: dict):
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with open(OCO_TRACKER, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def track_oco(symbol: str, oco_id: int, entry_price: float = 0.0):
    d = _read_oco_tracker()
    d[str(oco_id)] = {
        "symbol": symbol,
        "ts": int(time.time()),
        "entry": entry_price
    }
    _write_oco_tracker(d)
    print(f"[TRACK_OCO] Added OCO {oco_id} for {symbol} (entry: ${entry_price:.6f})")

def untrack_oco(oco_id: int):
    d = _read_oco_tracker()
    if str(oco_id) in d:
        d.pop(str(oco_id), None)
        _write_oco_tracker(d)
        print(f"[TRACK_OCO] Removed OCO {oco_id}")

def list_tracked_oco():
    d = _read_oco_tracker()
    return {int(k): v for k, v in d.items()}