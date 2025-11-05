# signal_trader.py
import asyncio, os, re, time, json, yaml
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass
from typing import Optional, Dict
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from telethon import TelegramClient, events
import ccxt

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


def read_settings() -> Settings:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return Settings(**yaml.safe_load(f))


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
        self.exchange.load_time_difference()  # auto-sync with Binance server
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


# --- Notifier ---
class Notifier:
    def __init__(self, chat_id: str):
        self.chat_id = chat_id

    async def send(self, client: TelegramClient, text: str):
        try:
            await client.send_message(int(self.chat_id), text)
        except Exception:
            await client.send_message(self.chat_id, text)


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

        # === Duplicate signal protection (30s window) ===
        if not hasattr(self, "_recent_signals"):
            self._recent_signals = []  # list of (symbol, entry, ts)

        now = time.time()
        self._recent_signals = [
            (sym, ent, ts)
            for (sym, ent, ts) in self._recent_signals
            if now - ts < 30
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
        free_q = 100.0 if s.dry_run else self.x.fetch_free_quote(symbol.split("/")[1])
        cap_pct = sig.capital_pct or s.capital_entry_pct_default
        spend = free_q * cap_pct
        if spend < s.min_notional_usdt:
            emit("skip", {"msg": f"Not enough quote: {free_q:.2f} to spend {spend:.2f}"})
            await self.n.send(self.tg, f"⚠️ Not enough quote balance")
            return

        last = self.x.fetch_price(symbol)
        acceptable = abs(last - sig.entry) / sig.entry <= s.max_slippage_pct
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

        try:
            if is_live:
                # === LIVE TRADING (real orders) ===
                from live_trade_executor import execute_market_buy, place_oco

                try:
                    # --- choose order type dynamically ---
                    if acceptable or not s.use_limit_if_slippage_exceeds:
                        # Within allowed deviation → market buy
                        filled_qty = execute_market_buy(symbol, spend)
                        await self.n.send(self.tg, f"✅ Live BUY filled {filled_qty} {symbol} ({mode_label})")
                    else:
                        # Too far from entry → place limit order at signal entry
                        usd_price = sig.entry
                        qty = spend / usd_price
                        amt_step, _ = self.x.lot_step_info(symbol)
                        qty = round_amt(qty, amt_step)
                        limit_order = self.x.create_limit_buy(symbol, qty, usd_price)
                        await self.n.send(self.tg, f"📉 Limit BUY placed {qty} {symbol} @ {usd_price}")

                        # Wait for fill up to timeout
                        timeout = s.limit_time_in_force_sec
                        start = time.time()
                        filled_qty = 0.0
                        while time.time() - start < timeout:
                            order = self.x.exchange.fetch_order(limit_order["id"], symbol)
                            if order["status"].lower() == "closed":
                                filled_qty = float(order["filled"])
                                await self.n.send(self.tg, f"✅ Limit BUY filled {filled_qty} {symbol} ({mode_label})")
                                break
                            await asyncio.sleep(5)
                        else:
                            self.x.cancel_order(symbol, limit_order["id"])
                            await self.n.send(self.tg, f"⌛ Limit not filled after {timeout}s — canceled.")
                            return

                    # --- Determine TP & SL (configurable overrides) ---
                    entry_price = float(sig.entry)

                    # Defaults from signal
                    tp = sig.tps.tp1
                    sl_trigger = sig.stop

                    # Apply TP override if enabled
                    if getattr(s, "override_tp_enabled", False):
                        tp = round(entry_price * (1.0 + float(s.override_tp_pct)), 8)
                        emit("info", {"msg": "Overriding TP from config", "tp": tp})

                    # Apply SL override if enabled
                    if getattr(s, "override_sl_enabled", False):
                        if getattr(s, "override_sl_as_absolute", False):
                            sl_trigger = round(entry_price - float(s.override_sl_pct), 8)
                        else:
                            sl_trigger = round(entry_price * (1.0 - float(s.override_sl_pct)), 8)
                        emit("info", {"msg": "Overriding SL from config", "sl": sl_trigger})

                    # Compute SL limit (0.1% below SL trigger)
                    sl_limit = sl_trigger - (sl_trigger * 0.001) if sl_trigger else None

                    # --- Telegram notice of overrides ---
                    override_msgs = []
                    if getattr(s, "override_tp_enabled", False):
                        override_msgs.append(f"TP → {tp}")
                    if getattr(s, "override_sl_enabled", False):
                        override_msgs.append(f"SL → {sl_trigger}")
                    if override_msgs:
                        await self.n.send(self.tg, "⚙️ Config override active: " + ", ".join(override_msgs))

                    # --- Safety check: ensure logical price order ---
                    if sl_trigger and not (tp > sig.entry > sl_trigger):
                        raise ValueError(
                            f"Invalid price relation: TP({tp}) > Entry({sig.entry}) > SL({sl_trigger}) required."
                        )

                    # --- Place OCO (TP + SL) ---
                    if sl_trigger and sl_limit:
                        oco = place_oco(
                            symbol,
                            "SELL",
                            f"{filled_qty:.8f}",
                            str(tp),
                            str(sl_trigger),
                            str(sl_limit)
                        )
                        await self.n.send(
                            self.tg,
                            f"🎯 OCO set → TP {tp}, SL {sl_trigger}/{sl_limit}\nOCO ID: {oco.get('orderListId', 'N/A')}"
                        )
                        emit("oco_placed", {
                            "symbol": symbol,
                            "tp": tp,
                            "sl": sl_trigger,
                            "oco_id": oco.get('orderListId')
                        })
                    else:
                        await self.n.send(self.tg, "⚠️ No SL provided — TP only.")

                except Exception as e:
                    await self.n.send(self.tg, f"❌ Live execution failed: {e}")
                    emit("error", {"msg": f"Live OCO exec failed: {e}"})

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
    print("✅ Telegram client started successfully.")

    notifier = Notifier(notify_chat)
    cfg = read_settings()
    binance = BinanceSpot(
    os.environ["BINANCE_API_KEY"],
    os.environ["BINANCE_API_SECRET"],
    cfg.dry_run,
    cfg.use_testnet
    )

    binance.prefer_usdc = (cfg.quote_asset.upper() == "USDC")
    trader = Trader(binance, client, notifier)

    print("✅ Notifier and Trader initialized.")
    emit("bot_start", {"msg": "Signal bot is up"})

    try:
        await notifier.send(client, "🤖 Signal bot is up and listening.")
        print("✅ Message sent successfully to notifier chat.")
    except Exception as e:
        print(f"❌ Failed to send notifier message: {e}")

    @client.on(events.NewMessage(chats=channel))
    async def handler(event):
        text = event.message.message or ""
        if not re.search(r'\bSignal\b|إشارة|Spot', text, flags=re.IGNORECASE):
            return
        emit("new_message", {"preview": text[:120]})
        from parsers.signal_parser import parse_signal
        sig = parse_signal(text)
        if not sig:
            emit("ignored", {"reason": "parse_failed"})
            return
        emit("parse_success", {"currency": sig.currency_display, "entry": sig.entry, "sl": sig.stop, "tp1": sig.tps.tp1})
        try:
            await trader.on_signal(sig)
        except Exception as e:
            emit("error", {"msg": repr(e)})
            await notifier.send(client, f"❌ Error: {e!r}")

    async def heart():
        while True:
            await asyncio.sleep(10)
            maybe_reload_settings()
            emit("heartbeat", {"dry_run": SETTINGS.dry_run})

    asyncio.create_task(heart())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
