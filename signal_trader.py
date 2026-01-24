# signal_trader.py
import asyncio, os, re, time, json, yaml
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass
from typing import Optional, Dict
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from telethon import TelegramClient, events
import ccxt
import csv, datetime, traceback, aiofiles
from live_trade_executor import (
    place_bracket_atomic,
    place_oco,
    execute_market_buy,
    place_stop_loss_market_sell,
    place_trailing_take_profit_market_sell,
    _fmt,
    _get_tick_and_step,
)
from binance.client import Client
import time
import math
from parsers.signal_parser import parse_signal, ParsedSignal, TPSet
from parsers.ai_signal_parser import AISignalParser

last_signal_ts = time.time()

# --- Local utils ---
RUNTIME_DIR = "runtime"
EVENTS_FILE = os.path.join(RUNTIME_DIR, "events.jsonl")
STATE_FILE = os.path.join(RUNTIME_DIR, "state.json")    
CONFIG_FILE = "config.yaml"
ALIASES_FILE = "token_aliases.json"
os.makedirs(RUNTIME_DIR, exist_ok=True)

         
def emit(event_type: str, payload: dict):
    """Append a compact JSON line for dashboard."""
    line = {"ts": int(time.time()), "type": event_type, **payload}
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def read_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_aliases() -> dict:
    """Load token aliases for name→symbol mapping."""
    try:
        with open(ALIASES_FILE, "r", encoding="utf-8") as f:
            aliases = json.load(f)
        # Normalize keys
        return {k.strip().upper(): v.upper() for k, v in aliases.items()}
    except Exception as e:
        emit("warning", {"msg": f"Failed to load aliases: {e}"})
        return {}


TOKEN_ALIASES = load_aliases()

# --- OCO Tracker Helpers ------------------------------------------------------
OCO_TRACKER = os.path.join(RUNTIME_DIR, "oco_tracker.json")

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
    """Track an OCO order for monitoring. Entry price is optional but helps with classification."""
    d = _read_oco_tracker()
    d[str(oco_id)] = {
        "symbol": symbol,
        "ts": int(time.time()),
        "entry": entry_price  # Store entry price for TP/SL classification
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
    fallback_to_name_search: bool = True  # now defaults True
    override_tp_enabled: bool = False
    override_tp_pct: float = 0.03
    override_sl_enabled: bool = False
    override_sl_pct: float = 0.01
    override_sl_as_absolute: bool = False
    default_sl_pct: float = 0.1
    # ===== Exit mode =====
    exit_mode: str = "fixed_oco"  # fixed_oco | trailing_tp
    # Trailing TP (percent as decimals)
    trailing_tp_activation_pct: float = 0.01  # +1%
    trailing_tp_pullback_pct: float = 0.005   # 0.5%


def read_settings() -> Settings:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return Settings(**yaml.safe_load(f))
    
def read_settings_dict() -> dict:
    """Read config.yaml as raw dict instead of Pydantic model."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

SETTINGS = read_settings()
_last_cfg_mtime = os.path.getmtime(CONFIG_FILE)


def maybe_reload_settings():
    global SETTINGS, _last_cfg_mtime
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
        if mtime != _last_cfg_mtime:
            SETTINGS = read_settings()
            _last_cfg_mtime = mtime
            emit("config_reloaded", {"msg": "config.yaml reloaded"})
    except Exception as e:
        emit("warning", {"msg": f"Failed to reload config: {e}"})

# --- Error logger -------------------------------------------------------------
async def log_error(msg: str):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs("runtime", exist_ok=True)
    async with aiofiles.open("runtime/errors.log", "a", encoding="utf-8") as f:
        await f.write(f"[{ts}] {msg}\n")

# --- PnL / exposure logger ----------------------------------------------------
def log_trade_pnl(symbol, side, entry, exit, qty, pnl_usd, status):
    """Append trade PnL/exposure to CSV."""
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

# --- Binance wrapper ---
class BinanceSpot:
    def __init__(self, key, secret, dry, use_testnet=False):
        self.dry = dry
        self.exchange = ccxt.binance({
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        # --- Time drift auto-correction ---
        try:
            diff = self.exchange.load_time_difference()
            print(f"✅ Binance time difference synced ({diff:.0f} ms)")
        except Exception as e:
            print(f"⚠️ Could not sync Binance time difference: {e}")

        if use_testnet:
            self.exchange.set_sandbox_mode(True)
        self.exchange.load_markets()

    def find_market(self, base, quote):
        base, quote = base.upper(), quote.upper()
        pair = f"{base}/{quote}"
        if pair in self.exchange.markets:
            return pair
        
        # Map USD to USDT
        if quote == "USD":
            alt = f"{base}/USDT"
            if alt in self.exchange.markets:
                return alt
        
        # Map USDT to USDC if user prefers USDC
        if quote == "USDT" and hasattr(self, 'prefer_usdc') and self.prefer_usdc:
            alt = f"{base}/USDC"
            if alt in self.exchange.markets:
                return alt
        
        return None

    def fetch_price(self, symbol):
        return float(self.exchange.fetch_ticker(symbol)["last"])

    def fetch_free_quote(self, quote):
        bal = self.exchange.fetch_balance()
        return float(bal["free"].get(quote, 0.0))

    def lot_step_info(self, symbol):
        m = self.exchange.market(symbol)
        amt_step = m["limits"]["amount"]["min"] or 0.00000001
        price_prec = m["precision"]["price"]
        step = 1 / (10 ** price_prec) if isinstance(price_prec, int) else 0.00000001
        return float(amt_step), float(step)

    def create_market_buy(self, symbol, amount):
        if self.dry:
            return {"id": "dry_buy", "status": "closed", "filled": amount, "price": self.fetch_price(symbol)}
        return self.exchange.create_order(symbol, "market", "buy", amount)

    def create_limit_buy(self, symbol, amount, price):
        if self.dry:
            return {"id": "dry_lmt_buy", "status": "open", "amount": amount, "price": price}
        return self.exchange.create_order(symbol, "limit", "buy", amount, price)

    def create_limit_sell(self, symbol, amount, price):
        if self.dry:
            return {"id": "dry_lmt_sell", "status": "open", "amount": amount, "price": price}
        return self.exchange.create_order(symbol, "limit", "sell", amount, price)

    def create_stop_market_sell(self, symbol, amount, stop_price):
        params = {"stopPrice": stop_price, "type": "STOP_LOSS"}
        if self.dry:
            return {"id": "dry_stop", "status": "open", "amount": amount, "stop": stop_price}
        return self.exchange.create_order(symbol, "market", "sell", amount, None, params)

    def cancel_order(self, symbol, order_id):
        if self.dry:
            return {"id": order_id, "status": "canceled"}
        return self.exchange.cancel_order(order_id, symbol)


def round_amt(q, step):
    if step <= 0:
        return q
    return float(Decimal(str(q)).quantize(Decimal(str(step)), rounding=ROUND_DOWN))

def get_safe_sell_qty(bin_client: Client, symbol: str, filled_qty: float, buffer: float = 0.999) -> float:
    """
    Compute a sell quantity that is <= free balance and rounded DOWN to step size.
    Prevents Binance -2010 insufficient balance due to fees/balance lag.
    """
    base_asset = symbol.split("/")[0].upper()

    # Retry a few times to let balance settle
    free_qty = 0.0
    for _ in range(10):  # up to ~5s total
        bal = bin_client.get_asset_balance(asset=base_asset) or {}
        free_qty = float(bal.get("free", 0) or 0.0)
        if free_qty > 0:
            break
        time.sleep(0.5)

    safe_qty = min(float(filled_qty), float(free_qty)) * buffer

    # Round DOWN to step
    _, step = _get_tick_and_step(symbol.replace("/", ""))
    safe_qty = math.floor(safe_qty / step) * step

    return float(safe_qty)

# --- Notifier ---
class Notifier:
    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self._entity_cache = None  # Cache the resolved entity
        print(f"[NOTIFIER INIT] Configured chat_id: {chat_id}")

    async def send(self, client: TelegramClient, text: str):
        """Send message to configured chat (handles bot, user, or channel)."""
        print(f"[NOTIFIER] Attempting to send message (length: {len(text)} chars)")
        print(f"[NOTIFIER] Target chat_id: {self.chat_id}")
        
        try:
            # Resolve entity once and cache it
            if self._entity_cache is None:
                print(f"[NOTIFIER] Resolving entity for: {self.chat_id}")
                try:
                    # Try as integer first
                    chat_id = int(self.chat_id)
                    print(f"[NOTIFIER] Parsed as integer: {chat_id}")
                except ValueError:
                    # Use as string (username)
                    chat_id = self.chat_id
                    print(f"[NOTIFIER] Using as string: {chat_id}")
                
                # Get and cache the entity
                self._entity_cache = await client.get_entity(chat_id)
                print(f"[NOTIFIER] ✅ Entity resolved: {type(self._entity_cache).__name__} (ID: {self._entity_cache.id})")
            
            # Send to cached entity
            print(f"[NOTIFIER] Sending to cached entity...")
            # prepend machine name
            cfg = read_settings_dict()
            prefix = cfg.get("machine_name", "").strip()
            final_text = f"💻 *{prefix}* — {text}" if prefix else text

            result = await client.send_message(self._entity_cache, final_text, parse_mode='markdown')
            print(f"[NOTIFIER] ✅ Message sent successfully! Message ID: {result.id}")
            
        except Exception as e:
            print(f"[NOTIFIER ERROR] ❌ Failed to send message: {e}")
            import traceback
            traceback.print_exc()
            
            # Try fallback without cache
            print(f"[NOTIFIER] Attempting fallback method 1 (direct int)...")
            try:
                cfg = read_settings_dict()
                prefix = cfg.get("machine_name", "").strip()
                final_text = f"💻 *{prefix}* — {text}" if prefix else text

                result = await client.send_message(int(self.chat_id), final_text, parse_mode='markdown')
                print(f"[NOTIFIER] ✅ Fallback 1 succeeded! Message ID: {result.id}")
            except Exception as e2:
                print(f"[NOTIFIER ERROR] ❌ Fallback 1 failed: {e2}")
                
                # Last resort: try as string
                print(f"[NOTIFIER] Attempting fallback method 2 (string)...")
                try:
                    cfg = read_settings_dict()
                    prefix = cfg.get("machine_name", "").strip()
                    final_text = f"💻 *{prefix}* — {text}" if prefix else text

                    result = await client.send_message(self.chat_id, final_text, parse_mode='markdown')
                    print(f"[NOTIFIER] ✅ Fallback 2 succeeded! Message ID: {result.id}")
                except Exception as e3:
                    print(f"[NOTIFIER ERROR] ❌ All attempts failed: {e3}")
                    import traceback
                    traceback.print_exc()


# --- Core Trader ---
from parsers.signal_parser import parse_signal, ParsedSignal, TPSet


class Trader:
    def __init__(self, binance: "BinanceSpot", tg_client, notifier):
        self.x = binance
        self.tg = tg_client
        self.n = notifier

    async def on_signal(self, sig: "ParsedSignal"):
        maybe_reload_settings()
        s = SETTINGS

        # === Signal debug ===
        emit("signal_parsed", {
            "currency": sig.currency_display,
            "entry": sig.entry,
            "sl": sig.stop,
            "tp1": sig.tps.tp1,
            "tp2": sig.tps.tp2,
            "tp3": sig.tps.tp3
        })

        await self.n.send(
            self.tg,
            f"🚀 *New Signal Detected!*\n"
            f"Currency: `{sig.currency_display}`\n"
            f"Entry: `${sig.entry}`\n"
            f"Stop Loss: `${sig.stop}`\n"
            f"TP1–TP3: `${sig.tps.tp1}`, `${sig.tps.tp2}`, `${sig.tps.tp3}`"
        )

        # === Pair Resolution ===
        base = sig.symbol_hint or sig.currency_display.split("/")[0].strip()
        base_clean = re.sub(r"[^A-Za-z0-9 ]", "", base).strip().upper()
        quote = SETTINGS.quote_asset.upper()
        symbol = None

        # 1️⃣ Direct slash
        if "/" in sig.currency_display:
            direct = sig.currency_display.replace(" ", "").upper()
            if direct in self.x.exchange.markets:
                symbol = direct

        # 2️⃣ Parentheses pattern
        if not symbol:
            paren_match = re.search(r"\(([A-Z0-9]+/[A-Z0-9]+)\)", sig.currency_display)
            if paren_match:
                candidate = paren_match.group(1).upper()
                if candidate in self.x.exchange.markets:
                    symbol = candidate

        # 3️⃣ Alias dictionary
        if not symbol:
            alias_symbol = TOKEN_ALIASES.get(base_clean)
            if alias_symbol:
                found = self.x.find_market(alias_symbol, quote)
                if found:
                    symbol = found

        # 4️⃣ Fallback to base/quote
        if not symbol:
            symbol = self.x.find_market(base_clean, quote)

        if not symbol:
            emit("error", {"msg": f"Pair not found for {sig.currency_display}"})
            await self.n.send(self.tg, f"❌ Pair not found for {sig.currency_display}")
            return

        await self.n.send(self.tg, f"✅ Pair resolved: *{symbol}*")

        # === Duplicate signal protection (180s window) ===
        if not hasattr(self, "_recent_signals"):
            self._recent_signals = []  # list of (symbol, entry, ts)

        now = time.time()
        self._recent_signals = [
            (sym, ent, ts)
            for (sym, ent, ts) in self._recent_signals
            if now - ts < 180
        ]

        # Normalize symbol name
        symbol_clean = symbol.replace(" ", "").upper()
        entry_price = round(float(sig.entry), 6)

        # Check duplicates
        for sym, ent, ts in self._recent_signals:
            if sym == symbol_clean and abs(ent - entry_price) < 1e-6:
                await self.n.send(
                    self.tg,
                    f"⚠️ Duplicate signal ignored for {symbol_clean} (entry {sig.entry})"
                )
                emit("skip_duplicate", {"symbol": symbol_clean, "entry": sig.entry})
                return

        # Record this signal
        self._recent_signals.append((symbol_clean, entry_price, now))

        # === Balance and sizing ===
        # Extract quote token (example: XRP/USDC → USDC)
        quote_token = symbol.split("/")[1]

        # Fetch REAL balance even in dry-run
        free_q = self.x.fetch_free_quote(quote_token)

        # --- Capital Entry Override Logic ---
        if s.override_capital_enabled:
            cap_pct = s.capital_entry_pct_default
        else:
            cap_pct = sig.capital_pct if sig.capital_pct is not None else s.capital_entry_pct_default

        spend = free_q * cap_pct

        # Minimum notional check  
        # In dry_run we do NOT skip it — we show simulation even if balance is low  
        if not s.dry_run and spend < s.min_notional_usdt:
            emit("skip", {"msg": f"Not enough quote: {free_q:.2f} to spend {spend:.2f}"})
            await self.n.send(self.tg, f"⚠️ Not enough quote balance")
            return

        # Fetch price & size
        last = self.x.fetch_price(symbol)
        acceptable = abs(last - sig.entry) / sig.entry <= s.max_slippage_pct

        # === Handle missing stop loss ===
        if sig.stop is None:
            # Use configurable default plus slippage
            default_sl = getattr(s, 'default_sl_pct', 0.10)  # fallback to 10% if not in config
            effective_sl_pct = default_sl + s.max_slippage_pct
            sig.stop = float(sig.entry) * (1.0 - effective_sl_pct)
            
            await self.n.send(
                self.tg,
                f"⚠️ No SL in signal — using default {effective_sl_pct*100:.2f}%: ${sig.stop:.6f}"
            )
            emit("info", {"msg": f"Auto-calculated SL for {sig.currency_display}: ${sig.stop:.6f}"})

        # --- If slippage exceeded: either LIMIT entry (wait/cancel) or SKIP ---
        if (not acceptable) and (not s.use_limit_if_slippage_exceeds):
            await self.n.send(
                self.tg,
                f"⏸️ Skipped trade (slippage too high)\n"
                f"Pair: `{symbol}`\n"
                f"Signal entry: `${float(sig.entry):.6f}`\n"
                f"Market: `${last:.6f}`\n"
                f"Max slippage: `{s.max_slippage_pct*100:.2f}%`"
            )
            emit("skip_slippage", {"symbol": symbol, "entry": float(sig.entry), "market": last})
            return

        amt_step, _ = self.x.lot_step_info(symbol)
        px_for_size = last if acceptable or not s.use_limit_if_slippage_exceeds else sig.entry
        amount = round_amt(spend / px_for_size, amt_step)

        if amount <= 0:
            emit("error", {"msg": "Computed amount zero"})
            await self.n.send(self.tg, "❌ Computed amount is zero")
            return

        # === Execution mode ===
        is_live = not s.dry_run
        is_testnet = getattr(s, "use_testnet", False)
        mode_label = "testnet" if (is_live and is_testnet) else "mainnet" if is_live else "sim"
        # 💥 DRY RUN = SIMULATION MODE (uses REAL BALANCE, but NO BUY)
        if s.dry_run:
            # Calculate what the TP/SL would be
            sim_tp = float(sig.tps.tp1)
            sim_sl = float(sig.stop)
            
            # If override is enabled, show what WOULD be overridden
            if s.override_tp_enabled:
                sim_tp = last * (1.0 + float(s.override_tp_pct))
            
            if s.override_sl_enabled:
                if s.override_sl_as_absolute:
                    sim_sl = last - float(s.override_sl_pct)
                else:
                    sim_sl = last * (1.0 - float(s.override_sl_pct))
            
            profit_pct = ((sim_tp / last) - 1) * 100
            loss_pct = ((last / sim_sl) - 1) * 100
            
            await self.n.send(
                self.tg,
                f"🧪 *SIMULATION ONLY — No order placed*\n"
                f"Pair: `{symbol}`\n"
                f"Balance: `{free_q:.4f}` {quote_token}\n"
                f"Capital %: `{cap_pct*100:.2f}%`\n"
                f"Spend: `{spend:.4f}` {quote_token}\n"
                f"Price: `{last:.6f}`\n"
                f"Amount: `{amount}` {symbol.split('/')[0]}\n"
                f"─────────────────\n"
                f"🎯 TP: `${sim_tp:.6f}` (+{profit_pct:.2f}%)\n"
                f"🛑 SL: `${sim_sl:.6f}` (-{loss_pct:.2f}%)\n"
                f"{'⚙️ Override enabled' if (s.override_tp_enabled or s.override_sl_enabled) else ''}"
            )
            emit("debug", {"msg": "STOP BEFORE BUY — SIMULATION MODE"})
            return

        try:
            if is_live:
                # === LIVE TRADING (real orders) === 
                try:
                    # Step 1: Determine INITIAL TP & SL from signal
                    initial_tp = float(sig.tps.tp1)
                    initial_sl = float(sig.stop)
                    
                    # Step 2: Determine if we should skip initial OCO
                    use_override_direct = getattr(s, "override_tp_enabled", False) or getattr(s, "override_sl_enabled", False)

                    # If slippage exceeded AND limit-entry enabled -> do LIMIT entry, wait TIF, cancel if not filled
                    if (not acceptable) and s.use_limit_if_slippage_exceeds:
                        import functools
                        from live_trade_executor import execute_limit_buy

                        tif = int(s.limit_time_in_force_sec)
                        loop = asyncio.get_running_loop()

                        # 🔔 Notify immediately when LIMIT is placed (thread-safe)
                        def notify_limit_placed(order_id):
                            loop.call_soon_threadsafe(
                                asyncio.create_task,
                                self.n.send(
                                    self.tg,
                                    (
                                        f"🟡 LIMIT order placed\n"
                                        f"Pair: `{symbol}`\n"
                                        f"Limit: `${float(sig.entry):.6f}`\n"
                                        f"TIF: `{tif}s`\n"
                                        f"OrderId: `{order_id}`"
                                    ),
                                ),
                            )

                        fn = functools.partial(
                            execute_limit_buy,
                            symbol=symbol,
                            usd_amount=spend,
                            limit_price=float(sig.entry),
                            tif_sec=tif,
                            on_placed=notify_limit_placed,
                        )

                        filled_qty, actual_fill_price, limit_oid = await asyncio.to_thread(fn)

                        # ⏸️ LIMIT not filled → canceled
                        if not filled_qty:
                            await self.n.send(
                                self.tg,
                                (
                                    f"⏸️ LIMIT order canceled (not filled)\n"
                                    f"Pair: `{symbol}`\n"
                                    f"Limit: `${float(sig.entry):.6f}`\n"
                                    f"Waited: `{tif}s`\n"
                                    f"OrderId: `{limit_oid}`"
                                ),
                            )
                            emit("limit_cancel", {"symbol": symbol, "order_id": limit_oid, "tif": tif})
                            return

                        # LIMIT filled → continue flow (OCO will be placed later)
                        res = {
                            "filled_qty": float(filled_qty),
                            "avg_price": float(actual_fill_price),
                            "tp": float(sig.tps.tp1),
                            "sl_trigger": float(sig.stop),
                            "sl_limit": None,
                            "oco_id": None,
                        }
                    else:
                        # Normal behavior (acceptable slippage): keep your original flow
                        if use_override_direct:
                            emit("info", {"msg": "Override mode enabled → skipping initial OCO, will use override OCO directly"})
                            from live_trade_executor import execute_market_buy
                            filled_qty, actual_fill_price = execute_market_buy(symbol, spend)
                            res = {
                                "filled_qty": filled_qty,
                                "avg_price": actual_fill_price,
                                "tp": float(sig.tps.tp1),
                                "sl_trigger": float(sig.stop),
                                "sl_limit": None,
                                "oco_id": None,
                            }
                        else:
                            # normal full bracket (market + OCO)
                            if getattr(s, "exit_mode", "fixed_oco") == "trailing_tp":
                                # market buy only (no OCO)
                                filled_qty, actual_fill_price = execute_market_buy(symbol, spend)
                                res = {
                                    "avg_price": float(actual_fill_price),
                                    "filled_qty": float(filled_qty),
                                    "tp": float(sig.tps.tp1),
                                    "sl_trigger": float(sig.stop),
                                    "oco_id": None,
                                }
                            else:
                                # normal full bracket (market + OCO)
                                res = place_bracket_atomic(
                                    symbol=symbol,
                                    spend_usd=spend,
                                    entry_hint=float(sig.entry),
                                    tp_price=float(sig.tps.tp1),
                                    sl_trigger=float(sig.stop),
                                )
                            
                    # Step 3: Get ACTUAL fill price and quantity
                    actual_fill_price = float(res['avg_price'])
                    filled_qty = float(res['filled_qty'])
                    
                    # Step 4: Check if we need to override TP/SL based on ACTUAL fill
                    needs_override = False
                    final_tp = res['tp']
                    final_sl = res['sl_trigger']
                    
                    # TP override
                    if getattr(s, "override_tp_enabled", False):
                        final_tp = round(actual_fill_price * (1.0 + float(s.override_tp_pct)), 8)
                        needs_override = True
                        emit("info", {
                            "msg": "TP overridden",
                            "fill": actual_fill_price,
                            "tp": final_tp,
                            "pct": s.override_tp_pct
                        })

                    # SL override
                    if getattr(s, "override_sl_enabled", False):
                        if getattr(s, "override_sl_as_absolute", False):
                            final_sl = round(actual_fill_price - float(s.override_sl_pct), 8)
                        else:
                            final_sl = round(actual_fill_price * (1.0 - float(s.override_sl_pct)), 8)
                        needs_override = True
                        emit("info", {
                            "msg": "SL overridden",
                            "fill": actual_fill_price,
                            "sl": final_sl,
                            "pct_or_abs": s.override_sl_pct
                        })

                    # ===== FIXED SL + TRAILING TP MODE =====
                    if getattr(s, "exit_mode", "fixed_oco") == "trailing_tp":
                        # 1. Use existing class references for clients
                        # We use self.x.exchange (ccxt) or a local binance-client for specific filters
                        bin_client = Client(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])
                        
                        safe_qty = get_safe_sell_qty(bin_client, symbol, float(filled_qty))
                        if safe_qty <= 0:
                            raise RuntimeError("Safe qty is 0; cannot place protection.")

                        # 2. Place the FIXED STOP LOSS on Binance (Capital Protection)
                        # This stays on the server even if your bot or internet dies.
                        sl_order = await asyncio.to_thread(
                            place_stop_loss_market_sell,
                            symbol,
                            float(safe_qty),
                            float(final_sl),
                        )
                        sl_id = sl_order.get("orderId")

                        # 3. Calculate Activation Price
                        activation_price = float(actual_fill_price) * (1.0 + float(s.trailing_tp_activation_pct))

                        await self.n.send(
                            self.tg,
                            (
                                f"✅ BUY filled `{safe_qty:.8f}` {symbol} @ `${actual_fill_price:.6f}`\n"
                                f"🛑 FIXED SL: `${float(final_sl):.6f}`\n"
                                f"🎯 TRAILING ACT: `${activation_price:.6f}` "
                                f"(Pullback: `{float(s.trailing_tp_pullback_pct)*100:.2f}%`)"
                            ),
                        )

                        # 4. Corrected Background Watcher with enhanced logging
                        async def activate_trailing_logic(sym, qty, act_px, current_sl_id):
                            # Emit start of monitoring to UI
                            emit("monitor_started", {"symbol": sym, "activation": act_px})
                            
                            while True:
                                try:
                                    # Fetch current price
                                    ticker = await asyncio.to_thread(self.x.exchange.fetch_ticker, sym)
                                    curr_px = float(ticker['last'])
                                    
                                    # (Optional) Log every check to UI for real-time monitoring
                                    # emit("price_check", {"symbol": sym, "price": curr_px, "target": act_px})
                                    
                                    if curr_px >= act_px:
                                        # Notify Telegram that activation price was hit
                                        await self.n.send(self.tg, f"🎯 *Activation Hit for {sym}*\nPrice reached `${curr_px:.6f}`. Swapping Fixed SL for Trailing TP.")

                                        # STEP A: Cancel the SL to unlock the coins
                                        await asyncio.to_thread(self.x.exchange.cancel_order, current_sl_id, sym)
                                        emit("sl_canceled", {"symbol": sym, "orderId": current_sl_id})
                                        
                                        # STEP B: Place Native Trailing TP
                                        # Passing None for act_px as discussed to ensure immediate server-side trailing
                                        trailing_order = await asyncio.to_thread(
                                            place_trailing_take_profit_market_sell,
                                            sym,
                                            qty,
                                            None, 
                                            float(s.trailing_tp_pullback_pct)
                                        )
                                        
                                        # Extract and notify details of the new trailing order
                                        tp_id = trailing_order.get("orderId", "Unknown")
                                        await self.n.send(
                                            self.tg, 
                                            f"🚀 *Trailing TP Active*\n"
                                            f"Symbol: `{sym}`\n"
                                            f"OrderID: `{tp_id}`\n"
                                            f"Trail Start: `${curr_px:.6f}`\n"
                                            f"Pullback: `{float(s.trailing_tp_pullback_pct)*100:.2f}%`"
                                        )
                                        
                                        emit("trailing_activated", {
                                            "symbol": sym, 
                                            "orderId": tp_id, 
                                            "startPrice": curr_px
                                        })
                                        break 
                                except Exception as e:
                                    error_msg = f"Watcher Error ({sym}): {e}"
                                    print(error_msg)
                                    emit("watcher_error", {"msg": error_msg})
                                await asyncio.sleep(2) 

                        asyncio.create_task(activate_trailing_logic(symbol, safe_qty, activation_price, sl_id))
                        return
                    
                    # Step 5: If override needed, cancel old OCO and place new one
                    if needs_override:
                        base_asset = symbol.split("/")[0].upper()
                        bin_client = Client(
                            os.environ["BINANCE_API_KEY"],
                            os.environ["BINANCE_API_SECRET"]
                        )

                        # --- Step A: cancel the old OCO if it exists ---
                        cancelled = False
                        if not res.get("oco_id"):
                            emit("info", {"msg": "No initial OCO to cancel (override-only mode)."})
                        else:
                            for i in range(5):
                                try:
                                    self.x.exchange.cancel_order(res["oco_id"], symbol)
                                    emit("info", {"msg": f"Cancelled original OCO {res['oco_id']} for override"})
                                    cancelled = True
                                    break
                                except Exception as e:
                                    msg = str(e)
                                    if "Unknown order" in msg or "not found" in msg:
                                        # OCO not yet visible — wait and retry
                                        time.sleep(0.4)
                                        continue
                                    else:
                                        emit("warning", {"msg": f"OCO cancel attempt {i+1} failed: {e}"})
                                        time.sleep(0.4)
                                        continue
                            if not cancelled:
                                emit("warning", {"msg": "Proceeding with override even though cancel not confirmed"})

                        # --- Step B: wait for balance to unlock ---
                        time.sleep(1.0)

                        # --- Step C: fetch exact free balance (post-cancel) ---
                        try:
                            bal = bin_client.get_asset_balance(asset=base_asset)
                            free_qty = float(bal["free"])
                            safe_qty = min(free_qty, filled_qty)
                            _, step = _get_tick_and_step(symbol.replace("/", ""))
                            safe_qty = math.floor(safe_qty / step) * step
                            print(f"[OCO DEBUG] override safe_qty={safe_qty} (free={free_qty}, filled={filled_qty})")
                        except Exception as e:
                            print(f"[WARN] Could not fetch free balance for {base_asset}: {e}, fallback to filled_qty")
                            safe_qty = filled_qty * 0.999

                        # --- Step D: compute final SL limit (0.01% below trigger) ---
                        final_sl_limit = round(final_sl * 0.9999, 8)

                        # --- Step E: place the new OCO with retry logic ---
                        for attempt in range(3):
                            try:
                                new_oco = place_oco(
                                    symbol,
                                    "SELL",
                                    _fmt(safe_qty),
                                    str(final_tp),
                                    str(final_sl),
                                    str(final_sl_limit)
                                )
                                new_oco_id = new_oco.get("orderListId")
                                print(f"[OCO SUCCESS] override placed qty={safe_qty} TP={final_tp} SL={final_sl}/{final_sl_limit}")
                                track_oco(symbol, new_oco_id)
                                emit("oco_tracked", {
                                    "symbol": symbol,
                                    "oco_id": new_oco_id,
                                    "msg": f"OCO tracked (ID {new_oco_id}) for {symbol}"
                                })

                                break  # success
                            except Exception as e:
                                msg = str(e).lower()
                                if "insufficient balance" in msg and attempt < 2:
                                    safe_qty *= 0.999
                                    safe_qty = math.floor(safe_qty / step) * step
                                    print(f"[OCO RETRY] Attempt {attempt+1}: insufficient balance, retrying with {safe_qty}")
                                    time.sleep(0.5)
                                    continue
                                raise

                        # --- Step F: confirmation message ---
                        new_oco_id = new_oco.get("orderListId")
                        profit_pct = ((final_tp / actual_fill_price) - 1) * 100
                        loss_pct = ((actual_fill_price / final_sl) - 1) * 100
                        await self.n.send(
                            self.tg,
                            (
                                f"✅ BUY filled {safe_qty:.8f} {symbol} @ ${actual_fill_price:.6f} ({mode_label})\n"
                                f"⚙️ Override applied:\n"
                                f"   • TP ${final_tp:.6f} (+{profit_pct:.2f}%)\n"
                                f"   • SL ${final_sl:.6f} (-{loss_pct:.2f}%)\n"
                                f"🆔 OCO ID: {new_oco_id}"
                            ),
                        )
                        emit("oco_overridden", {
                            "symbol": symbol,
                            "fill_price": actual_fill_price,
                            "filled_qty": safe_qty,
                            "tp": final_tp,
                            "sl_trigger": final_sl,
                            "oco_id": new_oco_id
                        })
                    else:
                        # --- FIX: if we entered via LIMIT, no OCO exists yet ---
                        if (not acceptable) and s.use_limit_if_slippage_exceeds and (res.get("oco_id") is None):
                            from live_trade_executor import place_oco_after_fill

                            oco_res = place_oco_after_fill(
                                symbol=symbol,
                                filled_qty=float(filled_qty),
                                fill_price=float(actual_fill_price),
                                tp_price=float(res["tp"]),
                                sl_trigger=float(res["sl_trigger"]),
                            )

                            res["oco_id"] = oco_res.get("oco_id")
                            res["sl_limit"] = oco_res.get("sl_limit")

                        # Safe SL limit formatting (avoid crash if None)
                        sl_lim = res.get("sl_limit")
                        sl_lim_txt = f"{float(sl_lim):.6f}" if sl_lim is not None else "N/A"

                        # No override — original OCO already in place (or just placed above)
                        await self.n.send(
                            self.tg,
                            (
                                f"✅ BUY filled {filled_qty:.8f} {symbol} @ ${actual_fill_price:.6f} ({mode_label})\n"
                                f"🎯 OCO set → TP ${float(res['tp']):.6f}, SL ${float(res['sl_trigger']):.6f}/{sl_lim_txt}\n"
                                f"🆔 OCO ID: {res['oco_id']}"
                            ),
                        )
                        emit(
                            "oco_placed",
                            {
                                "symbol": symbol,
                                "fill_price": actual_fill_price,
                                "filled_qty": filled_qty,
                                "tp": res["tp"],
                                "sl": res["sl_trigger"],
                                "oco_id": res["oco_id"],
                            },
                        )
                        # Track the OCO for monitoring
                        track_oco(symbol, res["oco_id"], actual_fill_price)
                        emit(
                            "oco_tracked",
                            {
                                "symbol": symbol,
                                "oco_id": res["oco_id"],
                                "msg": f"OCO tracked (ID {res['oco_id']}) for {symbol}",
                            },
                        )

                except Exception as e:
                    await self.n.send(self.tg, f"❌ Trade execution failed: {e}")
                    emit("error", {"msg": f"Trade execution failed: {e}"})
                    import traceback
                    await log_error(f"Trade error: {traceback.format_exc()}")

            else:
                # === SIMULATION MODE ===
                emit("entry_filled", {
                    "symbol": symbol,
                    "amount": amount,
                    "price": px_for_size,
                    "mode": mode_label
                })
                await self.n.send(
                    self.tg,
                    f"🟢 Simulated buy {symbol} {amount} @ ~{px_for_size:.6f}"
                )

        except Exception as e:
            emit("error", {"msg": f"Order placement failed: {e}"})
            await self.n.send(self.tg, f"❌ Order placement failed: {e}")

# Cache entity names for UI display
async def cache_telegram_entities(client, source_id, dest_id, notifier=None):
    """Cache Telegram entity names to runtime folder for UI display."""
    entity_cache = {}
    
    try:
        # Get source entity name
        try:
            source_entity = await client.get_entity(source_id if isinstance(source_id, int) else source_id)
            entity_cache["source"] = getattr(source_entity, 'title', getattr(source_entity, 'username', str(source_id)))
        except Exception as e:
            print(f"⚠️ Could not fetch source entity name: {e}")
            entity_cache["source"] = str(source_id)
        
        # Get destination entity name - use notifier's cached entity
        try:
            print(f"🔍 DEBUG: notifier={notifier}, has entity_cache={hasattr(notifier, 'entity_cache') if notifier else 'N/A'}, value={getattr(notifier, 'entity_cache', None) if notifier else 'N/A'}")
            if notifier and hasattr(notifier, '_entity_cache') and notifier._entity_cache:
                # Reuse the entity that notifier already fetched
                dest_entity = notifier._entity_cache
                entity_cache["destination"] = getattr(dest_entity, 'title', getattr(dest_entity, 'username', str(dest_id)))
                print(f"✅ Destination entity cached from notifier: {entity_cache['destination']}")
            else:
                # Try to fetch directly as fallback
                dest_entity = await client.get_entity(dest_id if isinstance(dest_id, int) else dest_id)
                entity_cache["destination"] = getattr(dest_entity, 'title', getattr(dest_entity, 'username', str(dest_id)))
                print(f"✅ Destination entity cached: {entity_cache['destination']}")
        except Exception as e:
            print(f"⚠️ Could not fetch destination entity name: {e}")
            entity_cache["destination"] = str(dest_id)
        
        # Save to file
        cache_file = os.path.join(RUNTIME_DIR, "telegram_entities.json")
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(entity_cache, f, ensure_ascii=False, indent=2)
        
        print(f"✅ Cached Telegram entities: {entity_cache}")
    except Exception as e:
        print(f"⚠️ Failed to cache Telegram entities: {e}")


# --- Main loop ---
async def main():
    load_dotenv()
    print("✅ Starting main()… environment loaded.")
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    channel = os.environ["TG_CHANNEL_ID_OR_USERNAME"]
    notify_chat = os.environ["TG_NOTIFY_CHAT_ID"]

    session_path = os.path.join(os.path.dirname(__file__), "signals_session")
    print(f"[Telegram] Using session file: {session_path}.session")
    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()

    # --- Prefetch and normalize entity ---
    chat_id_str = os.getenv("TG_CHANNEL_ID_OR_USERNAME")
    channel_to_listen = None  # This will be used in the handler
    
    if chat_id_str:
        try:
            # Try as integer first
            try:
                channel_to_listen = int(chat_id_str)  # Convert to int!
                resolved_entity = await client.get_entity(channel_to_listen)
                print(f"✅ Entity resolved as ID: {channel_to_listen} → {getattr(resolved_entity, 'title', resolved_entity)}")
            except ValueError:
                # It's a username string
                channel_to_listen = chat_id_str
                resolved_entity = await client.get_entity(chat_id_str)
                print(f"✅ Entity resolved as username: {chat_id_str} → {getattr(resolved_entity, 'title', resolved_entity)}")
        except Exception as e:
            print(f"⚠️ Could not prefetch entity ({chat_id_str}): {e}")
            print(f"   Will try using it directly anyway...")
            # Fallback: try to use as-is
            try:
                channel_to_listen = int(chat_id_str)
            except ValueError:
                channel_to_listen = chat_id_str
    else:
        print("⚠️ TG_CHANNEL_ID_OR_USERNAME not found in .env")
        channel_to_listen = channel  # Fallback to original string

    print("✅ Telegram client started successfully.")

    notifier = Notifier(notify_chat)        
    # Send startup message to cache destination entity
    try:
        await notifier.send(client, "✅ Bot connected and ready!")
        print("✅ Sent startup message to destination chat")
    except Exception as e:
        print(f"⚠️ Could not send startup message: {e}")

    cfg = read_settings()
    binance = BinanceSpot(
        os.environ["BINANCE_API_KEY"],
        os.environ["BINANCE_API_SECRET"],
        cfg.dry_run,
        cfg.use_testnet
    )
    binance.prefer_usdc = (cfg.quote_asset.upper() == "USDC")
    trader = Trader(binance, client, notifier)
    # Initialize AI parser
    try:
        ai_parser = AISignalParser()
        print("AI PARSER: Initialized successfully with Groq")
    except Exception as e:
        ai_parser = None
        print(f"AI PARSER: Failed to initialize - {e}")

    # Cache entity names for UI
    await cache_telegram_entities(client, channel_to_listen, notify_chat, notifier)
    # --- SAFETY TASKS: STOP-GAP FLATTEN AUDIT ---
    async def audit_positions_loop():
        interval = float(read_settings_dict().get("flatten_check_interval_min", 10)) * 60
        while True:
            try:
                open_orders = binance.exchange.fetch_open_orders()
                # Group by symbol
                missing = {}
                for o in open_orders:
                    if o["type"] not in ("TAKE_PROFIT_LIMIT", "STOP_LOSS_LIMIT"):
                        continue
                    missing.setdefault(o["symbol"], set()).add(o["type"])
                for sym, types in missing.items():
                    if not {"TAKE_PROFIT_LIMIT", "STOP_LOSS_LIMIT"}.issubset(types):
                        await notifier.send(
                            client,
                            f"⚠️ Audit: {sym} missing "
                            f"{'TP' if 'TAKE_PROFIT_LIMIT' not in types else 'SL'} order!"
                        )
                await asyncio.sleep(interval)
            except Exception as e:
                await log_error(f"flatten audit error: {e}")
                await asyncio.sleep(interval)

    asyncio.create_task(audit_positions_loop())
    
    # --- SAFETY TASK: HEARTBEAT WATCHDOG ---
    heartbeat_max = float(read_settings_dict().get("heartbeat_max_idle_min", 30))
    print(f"⏱️ Heartbeat watchdog started (max idle {heartbeat_max} min)")

    async def heartbeat_watchdog():
        global last_signal_ts
        max_idle = heartbeat_max * 60
        while True:
            await asyncio.sleep(max_idle / 2)
            idle = time.time() - last_signal_ts
            if idle > max_idle:
                await notifier.send(
                    client,
                    f"⚠️ No signals received for {int(idle/60)} minutes!"
                )
                await log_error(f"⚠️ Heartbeat: no signals for {int(idle/60)} minutes")

    asyncio.create_task(heartbeat_watchdog())

    # --- EVENT HANDLER ---
    print(f"👂 Listening to channel: {channel_to_listen!r} (type: {type(channel_to_listen).__name__})")
    
    # FIXED: Register debug handler to see ALL messages
    @client.on(events.NewMessage(chats=channel_to_listen))
    async def debug_all_messages(event):
        """Debug: log every single message received"""
        try:
            chat = await event.get_chat()
            chat_name = getattr(chat, 'title', str(chat))
            print(f"[🔍 RAW] From '{chat_name}' (ID: {chat.id})")
            print(f"[🔍 TEXT] {event.raw_text[:200]!r}")
        except Exception as e:
            print(f"[🔍 ERROR] Debug handler failed: {e}")
    
    @client.on(events.NewMessage(chats=channel_to_listen))
    async def handler(event):
        text = event.message.message or ""
        global last_signal_ts
        last_signal_ts = time.time()

        print(f"[✅ HANDLER] Message received, length: {len(text)} chars")

        # Check for obvious signal keywords
        has_keywords = re.search(r'signal|إشارة|spot|coin|entry|buy|sell|trade', text, flags=re.IGNORECASE)
        
        if not has_keywords:
            print("[⚠️ NO KEYWORDS] Trying AI parser anyway...")
            # Try AI parser directly since no keywords found
            if ai_parser is not None:
                start_time = time.time()
                sig = ai_parser.parse(text)
                parse_time = (time.time() - start_time) * 1000
                print(f"[🤖 AI PARSE] Completed in {parse_time:.0f}ms - {'SUCCESS' if sig else 'FAILED'}")
                
                if sig:
                    # AI successfully parsed it - continue to trade execution
                    print(f"[✅ PARSED BY AI] {sig.currency_display} @ {sig.entry}")
                    emit("ai_parse_success", {"currency": sig.currency_display, "entry": sig.entry})
                else:
                    print("[⏭️ SKIP] AI also couldn't parse this message")
                    emit("ignored", {"reason": "no_keywords_and_ai_failed", "preview": text[:100]})
                    return
            else:
                print("[⏭️ SKIP] No keywords and AI parser not available")
                emit("ignored", {"reason": "no_keyword", "preview": text[:100]})
                return
        else:
            print("[✅ KEYWORD] Found signal keyword!")
            emit("new_message", {"preview": text[:120]})

        # Try regex parser first (fast)
        from parsers.signal_parser import parse_signal
        start_time = time.time()
        sig = parse_signal(text)
        parse_time = (time.time() - start_time) * 1000
        print(f"[🔍 REGEX PARSE] Completed in {parse_time:.0f}ms - {'SUCCESS' if sig else 'FAILED'}")

        # If regex fails, try AI parser as fallback
        if not sig and ai_parser is not None:
            print("[🤖 AI PARSE] Attempting with Groq...")
            start_time = time.time()
            sig = ai_parser.parse(text)
            parse_time = (time.time() - start_time) * 1000
            print(f"[🤖 AI PARSE] Completed in {parse_time:.0f}ms - {'SUCCESS' if sig else 'FAILED'}")
            
            if sig:
                emit("ai_parse_success", {"currency": sig.currency_display, "entry": sig.entry})
        
        if not sig:
            print("[❌ PARSE] Both regex and AI parsing failed")
            emit("ignored", {"reason": "parse_failed", "msg": f"Ignored unparseable signal: {text[:80]}..."})
            return

        print(f"[✅ PARSED] {sig.currency_display} @ {sig.entry}")
        emit("parse_success", {
            "currency": sig.currency_display,
            "entry": sig.entry,
            "sl": sig.stop,
            "tp1": sig.tps.tp1
        })
        
        try:
            await trader.on_signal(sig)
        except Exception as e:
            print(f"[❌ TRADE] Error executing trade: {e}")
            emit("error", {"msg": repr(e)})
            await notifier.send(client, f"❌ Error: {e!r}")

    # Heartbeat task
    # async def heart():
        # while True:
            # await asyncio.sleep(10)
            # maybe_reload_settings()
            # await emit_system_status()

    # asyncio.create_task(heart())

    # --- SAFETY TASK: FLATTEN WATCHDOG ---
    async def flatten_watchdog():
        """Checks if any position has no TP/SL protection, but skips dust < $10."""
        cfg_dict = read_settings_dict()
        interval = float(cfg_dict.get("flatten_check_interval_min", 10)) * 60
        MIN_NOTIONAL = 10.0  # <<< NEW: minimum USD value before flatten

        print(f"🛡️ Flatten watchdog started (every {interval/60:.1f} min, min notional ${MIN_NOTIONAL})")
        await asyncio.sleep(interval)  # wait before first run

        while True:
            try:
                # --- Fetch balances ---
                bal = binance.exchange.fetch_balance()
                open_assets = {
                    a: b for a, b in bal["free"].items()
                    if a not in ("USDT", "USDC", "BUSD") and b > 0
                }

                for asset, qty in open_assets.items():
                    # Try both markets to find a valid trading symbol
                    possible_quotes = ["USDC", "USDT"]
                    sym = None
                    price = None

                    for quote in possible_quotes:
                        pair = f"{asset}/{quote}"
                        if pair in binance.exchange.markets:
                            sym = pair
                            try:
                                price = float(binance.exchange.fetch_ticker(pair)["last"])
                            except Exception:
                                price = None
                            break

                    if not sym or price is None:
                        continue  # cannot price the asset, skip

                    # --- NEW: Skip tiny positions under $10 ---
                    notional = qty * price
                    if notional < MIN_NOTIONAL:
                        print(f"⏭️ [SKIP] {sym} value ${notional:.2f} < ${MIN_NOTIONAL} (dust ignored)")
                        continue

                    # --- Check exchange min lot ---
                    tick, step = _get_tick_and_step(sym.replace("/", ""))
                    if qty < step:
                        continue

                    # --- Fetch open orders for symbol ---
                    try:
                        orders = binance.exchange.fetch_open_orders(sym)
                    except Exception:
                        continue

                    tp_present = any("TAKE_PROFIT" in o["type"].upper() for o in orders)
                    sl_present = any("STOP_LOSS" in o["type"].upper() for o in orders)

                    # Flatten only if BOTH missing
                    if not tp_present and not sl_present:
                        msg = f"⚠️ Flatten: {sym} missing TP/SL — flattening {qty:.4f}"
                        print(msg)
                        emit("flatten_check", {
                            "symbol": sym,
                            "qty": qty,
                            "msg": f"Flatten check triggered for {sym} (qty {qty:.4f})"
                        })
                        await notifier.send(client, msg)

                        try:
                            binance.exchange.create_order(sym, "market", "sell", qty)
                            emit("flatten_sell", {
                                "symbol": sym,
                                "qty": qty,
                                "msg": f"Flatten SELL executed for {sym} ({qty:.4f})"
                            })
                        except Exception as e:
                            emit("flatten_error", {"symbol": sym, "error": str(e)})
                            await log_error(f"Flatten error: {sym} {e}")

                await asyncio.sleep(interval)

            except Exception as e:
                await log_error(f"flatten watchdog error: {e}")
                await asyncio.sleep(interval)

    # --- ORDER MONITORING TASK: Detect TP/SL hits ---
    async def monitor_orders_loop():
        """Monitor open OCO orders and notify when TP or SL is hit."""
        tracked_orders = {}  # {order_id: {"symbol": "BTC/USDT", "type": "TP", "price": 50000}}
        
        while True:
            try:
                await asyncio.sleep(15)  # Check every 15 seconds
                
                # Fetch all open orders
                open_orders = binance.exchange.fetch_open_orders()
                open_order_ids = {o['id'] for o in open_orders}
                
                # Track new OCO orders
                for order in open_orders:
                    if order['id'] not in tracked_orders:
                        order_type = "TP" if "TAKE_PROFIT" in order['type'] else "SL" if "STOP_LOSS" in order['type'] else None
                        if order_type:
                            tracked_orders[order['id']] = {
                                "symbol": order['symbol'],
                                "type": order_type,
                                "price": order.get('stopPrice') or order.get('price'),
                                "amount": order['amount']
                            }
                
                # Detect filled orders (no longer in open orders)
                filled_ids = set(tracked_orders.keys()) - open_order_ids
                for order_id in filled_ids:
                    info = tracked_orders.pop(order_id)
                    
                    # Fetch the order to confirm it was filled (not just canceled)
                    try:
                        order = binance.exchange.fetch_order(order_id, info['symbol'])
                        if order['status'] == 'closed' and order['filled'] > 0:
                            # Cancel any remaining STOP_LOSS / TAKE_PROFIT orders for this symbol
                            try:
                                still_open = binance.exchange.fetch_open_orders(info['symbol'])
                                for oo in still_open:
                                    ot = (oo.get("type") or "").upper()
                                    if ("STOP_LOSS" in ot) or ("TAKE_PROFIT" in ot):
                                        try:
                                            binance.exchange.cancel_order(oo["id"], info["symbol"])
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                            # Order was filled!
                            emoji = "🎯" if info['type'] == "TP" else "🛑"
                            msg = (
                                f"{emoji} **{info['type']} HIT!**\n"
                                f"Symbol: `{info['symbol']}`\n"
                                f"Price: `${info['price']:.6f}`\n"
                                f"Amount: `{info['amount']:.8f}`\n"
                                f"Status: Filled ✅"
                            )
                            await notifier.send(client, msg)
                            emit("order_filled", {
                                "symbol": info['symbol'],
                                "type": info['type'],
                                "price": info['price'],
                                "amount": info['amount']
                            })
                    except Exception as e:
                        print(f"[ORDER MONITOR] Error fetching order {order_id}: {e}")
                        
            except Exception as e:
                await log_error(f"order monitor error: {e}")
                await asyncio.sleep(15)

    # --- OCO MONITOR LOOP (Spot-compatible version) -----------------------------
    async def monitor_tracked_oco_loop():
        """Poll order history instead of get_oco_order; works for Spot/subaccounts."""
        bin_client = Client(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])

        while True:
            try:
                tracked = list_tracked_oco()
                if not tracked:
                    await asyncio.sleep(5)
                    continue

                for oco_id, meta in list(tracked.items()):
                    symbol = meta["symbol"].replace("/", "").upper()

                    try:
                        # fetch recent order history for this symbol
                        recent = bin_client.get_all_orders(symbol=symbol, limit=10)
                        tp_hit = sl_hit = None

                        for o in reversed(recent):
                            # Binance tags OCO children with orderListId
                            if str(o.get("orderListId")) != str(oco_id):
                                continue
                            if (o.get("status") or "").upper() == "FILLED":
                                o_type = (o.get("type") or "").upper()
                                o_price = o.get("price") or o.get("stopPrice") or "0"
                                o_exec = float(o.get("executedQty", 0) or 0.0)
                                if o_exec <= 0:
                                    continue
                                # --- classify by both type and price relation ---
                                fill_price = float(o_price)
                                entry_price = float(meta.get("entry", 0) or 0)  # store entry when you track OCOs if possible

                                # fallback if entry not stored: decide by relative distance to mid-price
                                if entry_price == 0:
                                    entry_price = fill_price  # neutral default

                                if "STOP" in o_type:
                                    sl_hit = (o_price, o_exec)
                                elif "TAKE_PROFIT" in o_type:
                                    tp_hit = (o_price, o_exec)
                                else:
                                    # unknown / mis-labelled type → decide by price vs entry
                                    if fill_price < entry_price:
                                        sl_hit = (o_price, o_exec)
                                    else:
                                        tp_hit = (o_price, o_exec)

                        if tp_hit:
                            p, q = tp_hit
                            await notifier.send(
                                client,
                                f"🎯 **TP HIT!**\nSymbol: `{meta['symbol']}`\nPrice: `${float(p):.6f}`\nQty: `{q}`"
                            )
                            emit("order_filled", {"symbol": meta["symbol"], "type": "TP", "price": float(p), "amount": q})
                            untrack_oco(oco_id)
                            continue

                        if sl_hit:
                            p, q = sl_hit
                            await notifier.send(
                                client,
                                f"🛑 **SL HIT!**\nSymbol: `{meta['symbol']}`\nPrice: `${float(p):.6f}`\nQty: `{q}`"
                            )
                            emit("order_filled", {"symbol": meta["symbol"], "type": "SL", "price": float(p), "amount": q})
                            untrack_oco(oco_id)
                            continue

                    except Exception as e:
                        await log_error(f"OCO history poll error {meta['symbol']} ({oco_id}): {e}")

                await asyncio.sleep(5)

            except Exception as e:
                await log_error(f"OCO monitor loop fatal error: {e}")
                await asyncio.sleep(5)

    
    # --- SAFETY TASK STARTER: delay watchdog startup ---
    async def start_flatten_watchdog():
        cfg_dict = read_settings_dict()
        interval = float(cfg_dict.get("flatten_check_interval_min", 10)) * 60
        print(f"🕒 Delaying flatten watchdog start by {interval/60:.1f} min...")
        await asyncio.sleep(interval)  # wait full interval before enabling
        print("🛡️ Flatten watchdog now active.")
        asyncio.create_task(flatten_watchdog())

    asyncio.create_task(start_flatten_watchdog())

    # --- BACKEND PING LOOP (for UI health monitoring) ---
    async def backend_ping_loop():
        """Writes runtime/backend.ping every 10 seconds so ui_server knows backend is alive."""
        path = os.path.join("runtime", "backend.ping")
        while True:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(str(time.time()))
                os.utime(path, None)
            except Exception as e:
                await log_error(f"backend ping error: {e}")
            await asyncio.sleep(10)  # ✅ ping every 10 seconds

    asyncio.create_task(backend_ping_loop())
    
    # Start the order monitor
    asyncio.create_task(monitor_orders_loop())
    asyncio.create_task(monitor_tracked_oco_loop())

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
