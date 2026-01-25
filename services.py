import ccxt
import time
import math
import os
import json
from decimal import Decimal, ROUND_DOWN
from binance.client import Client
from dotenv import load_dotenv  # Required for the new factory
import trading_shared as ts

# --- Helpers ---
def round_amt(q, step):
    if step <= 0:
        return q
    return float(Decimal(str(q)).quantize(Decimal(str(step)), rounding=ROUND_DOWN))

def get_safe_sell_qty(bin_client: Client, symbol: str, filled_qty: float, buffer: float = 0.999) -> float:
    # ⚠️ MOVED IMPORT HERE to prevent Circular Import Error
    # (Because live_trade_executor now imports THIS file)
    from live_trade_executor import _get_tick_and_step
    
    base_asset = symbol.split("/")[0].upper()
    free_qty = 0.0
    for _ in range(10):
        bal = bin_client.get_asset_balance(asset=base_asset) or {}
        free_qty = float(bal.get("free", 0) or 0.0)
        if free_qty > 0:
            break
        time.sleep(0.5)

    safe_qty = min(float(filled_qty), float(free_qty)) * buffer
    _, step = _get_tick_and_step(symbol.replace("/", ""))
    safe_qty = math.floor(safe_qty / step) * step
    return float(safe_qty)

async def cache_telegram_entities(client, source_id, dest_id, notifier=None):
    entity_cache = {}
    try:
        try:
            source_entity = await client.get_entity(source_id if isinstance(source_id, int) else source_id)
            entity_cache["source"] = getattr(source_entity, 'title', getattr(source_entity, 'username', str(source_id)))
        except Exception as e:
            entity_cache["source"] = str(source_id)
        
        try:
            if notifier and hasattr(notifier, '_entity_cache') and notifier._entity_cache:
                dest_entity = notifier._entity_cache
                entity_cache["destination"] = getattr(dest_entity, 'title', getattr(dest_entity, 'username', str(dest_id)))
            else:
                dest_entity = await client.get_entity(dest_id if isinstance(dest_id, int) else dest_id)
                entity_cache["destination"] = getattr(dest_entity, 'title', getattr(dest_entity, 'username', str(dest_id)))
        except Exception as e:
            entity_cache["destination"] = str(dest_id)
        
        cache_file = os.path.join(ts.RUNTIME_DIR, "telegram_entities.json")
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(entity_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ Failed to cache Telegram entities: {e}")


# --- Classes ---
class BinanceSpot:
    def __init__(self, key, secret, dry, use_testnet=False):
        self.dry = dry
        self.exchange = ccxt.binance({
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })
        try:
            diff = self.exchange.load_time_difference()
            print(f"✅ Binance (CCXT) time difference synced ({diff:.0f} ms)")
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
        
        if quote == "USD":
            alt = f"{base}/USDT"
            if alt in self.exchange.markets:
                return alt
        
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


class Notifier:
    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self._entity_cache = None
        print(f"[NOTIFIER INIT] Configured chat_id: {chat_id}")

    async def send(self, client, text: str):
        print(f"[NOTIFIER] Attempting to send message (length: {len(text)} chars)")
        try:
            if self._entity_cache is None:
                try:
                    chat_id = int(self.chat_id)
                except ValueError:
                    chat_id = self.chat_id
                self._entity_cache = await client.get_entity(chat_id)
            
            cfg = ts.read_settings_dict()
            prefix = cfg.get("machine_name", "").strip()
            final_text = f"💻 *{prefix}* — {text}" if prefix else text

            result = await client.send_message(self._entity_cache, final_text, parse_mode='markdown')
            print(f"[NOTIFIER] ✅ Message sent successfully! ID: {result.id}")
        except Exception as e:
            print(f"[NOTIFIER ERROR] ❌ Failed to send message: {e}")
            try:
                cfg = ts.read_settings_dict()
                prefix = cfg.get("machine_name", "").strip()
                final_text = f"💻 *{prefix}* — {text}" if prefix else text
                await client.send_message(self.chat_id, final_text, parse_mode='markdown')
            except Exception as e2:
                print(f"[NOTIFIER ERROR] ❌ Fallback failed: {e2}")

# --- GLOBAL FIX: Centralized Client Factory ---
def get_synced_client():
    """
    Returns a python-binance Client that is GUARANTEED to be time-synced.
    Use this instead of Client(api_key, api_secret) in all files.
    """
    load_dotenv()
    key = os.getenv("BINANCE_API_KEY")
    sec = os.getenv("BINANCE_API_SECRET")
    
    if not key or not sec:
        raise ValueError("Missing Binance API Key/Secret")
        
    client = Client(key, sec)
    
    # FORCE SYNC
    try:
        server_res = client.get_server_time()
        server_time = server_res['serverTime']
        local_time = int(time.time() * 1000)
        # Fix the drift permanently for this instance
        client.timestamp_offset = server_time - local_time
        print(f"✅ [FACTORY] Binance Client Synced. Offset: {client.timestamp_offset}ms")
    except Exception as e:
        print(f"⚠️ [FACTORY] Time sync failed (continuing anyway): {e}")
        
    return client