# signal_trader.py
import asyncio
import os
import re
import time
import traceback
from dotenv import load_dotenv
from telethon import TelegramClient, events
from binance.client import Client

# New imports from refactored modules
import trading_shared as ts
import services as sv
from trader_core import Trader  # <--- Now importing your full Logic
from parsers.signal_parser import parse_signal
from parsers.ai_signal_parser import AISignalParser
from live_trade_executor import _get_tick_and_step

last_signal_ts = time.time()

# --- Background Tasks ---
async def audit_positions_loop(binance: sv.BinanceSpot, notifier: sv.Notifier, client):
    interval = float(ts.read_settings_dict().get("flatten_check_interval_min", 10)) * 60
    while True:
        try:
            open_orders = binance.exchange.fetch_open_orders()
            missing = {}
            for o in open_orders:
                if o["type"] not in ("TAKE_PROFIT_LIMIT", "STOP_LOSS_LIMIT"):
                    continue
                missing.setdefault(o["symbol"], set()).add(o["type"])
            for sym, types in missing.items():
                if not {"TAKE_PROFIT_LIMIT", "STOP_LOSS_LIMIT"}.issubset(types):
                    await notifier.send(client, f"⚠️ Audit: {sym} missing side of OCO")
            await asyncio.sleep(interval)
        except Exception as e:
            await ts.log_error(f"Audit error: {e}")
            await asyncio.sleep(interval)

async def heartbeat_watchdog(notifier: sv.Notifier, client):
    global last_signal_ts
    while True:
        cfg = ts.read_settings_dict()
        heartbeat_max = float(cfg.get("heartbeat_max_idle_min", 30))
        await asyncio.sleep(heartbeat_max * 30) # Check halfway
        idle = time.time() - last_signal_ts
        if idle > (heartbeat_max * 60):
            await notifier.send(client, f"⚠️ No signals for {int(idle/60)} minutes!")
            await ts.log_error("Heartbeat timeout")

async def flatten_watchdog(binance: sv.BinanceSpot, notifier: sv.Notifier, client):
    cfg_dict = ts.read_settings_dict()
    interval = float(cfg_dict.get("flatten_check_interval_min", 10)) * 60
    MIN_NOTIONAL = 10.0
    print(f"🛡️ Flatten watchdog waiting {interval}s to start...")
    await asyncio.sleep(interval)

    while True:
        try:
            bal = binance.exchange.fetch_balance()
            open_assets = {a: b for a, b in bal["free"].items() if a not in ("USDT", "USDC", "BUSD") and b > 0}

            for asset, qty in open_assets.items():
                sym = None
                price = None
                for quote in ["USDC", "USDT"]:
                    pair = f"{asset}/{quote}"
                    if pair in binance.exchange.markets:
                        sym = pair
                        try:
                            price = float(binance.exchange.fetch_ticker(pair)["last"])
                        except: pass
                        break
                
                if not sym or price is None: continue
                if (qty * price) < MIN_NOTIONAL: continue

                _, step = _get_tick_and_step(sym.replace("/", ""))
                if qty < step: continue

                try:
                    orders = binance.exchange.fetch_open_orders(sym)
                except: continue

                tp_present = any("TAKE_PROFIT" in o["type"].upper() for o in orders)
                sl_present = any("STOP_LOSS" in o["type"].upper() for o in orders)

                if not tp_present and not sl_present:
                    msg = f"⚠️ Flatten: {sym} missing TP/SL — flattening {qty:.4f}"
                    await notifier.send(client, msg)
                    try:
                        binance.exchange.create_order(sym, "market", "sell", qty)
                    except Exception as e:
                        await ts.log_error(f"Flatten error {sym}: {e}")

            await asyncio.sleep(interval)
        except Exception as e:
            await ts.log_error(f"Flatten loop error: {e}")
            await asyncio.sleep(interval)

async def monitor_orders_loop(binance: sv.BinanceSpot, notifier: sv.Notifier, client):
    tracked_orders = {}
    while True:
        try:
            await asyncio.sleep(15)
            open_orders = binance.exchange.fetch_open_orders()
            open_ids = {o['id'] for o in open_orders}

            for order in open_orders:
                if order['id'] not in tracked_orders:
                    t = "TP" if "TAKE_PROFIT" in order['type'] else "SL" if "STOP_LOSS" in order['type'] else None
                    if t:
                        tracked_orders[order['id']] = {
                            "symbol": order['symbol'], "type": t,
                            "price": order.get('stopPrice') or order.get('price'),
                            "amount": order['amount']
                        }

            filled_ids = set(tracked_orders.keys()) - open_ids
            for oid in filled_ids:
                info = tracked_orders.pop(oid)
                try:
                    order = binance.exchange.fetch_order(oid, info['symbol'])
                    if order['status'] == 'closed' and order['filled'] > 0:
                        try:
                            # Cancel opposite side
                            still_open = binance.exchange.fetch_open_orders(info['symbol'])
                            for oo in still_open:
                                if "STOP_LOSS" in oo.get("type", "") or "TAKE_PROFIT" in oo.get("type", ""):
                                    binance.exchange.cancel_order(oo["id"], info["symbol"])
                        except: pass
                        
                        emoji = "🎯" if info['type'] == "TP" else "🛑"
                        await notifier.send(client, f"{emoji} **{info['type']} HIT!**\nSymbol: {info['symbol']}\nPrice: ${float(info['price']):.6f}\nQty: {info['amount']}")
                        ts.emit("order_filled", {"symbol": info['symbol'], "type": info['type']})
                except Exception: pass
        except Exception as e:
            await ts.log_error(f"Order monitor error: {e}")

async def monitor_tracked_oco_loop(notifier: sv.Notifier, client):
    bin_client = Client(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])
    while True:
        try:
            tracked = ts.list_tracked_oco()
            if not tracked:
                await asyncio.sleep(5)
                continue

            for oco_id, meta in list(tracked.items()):
                symbol = meta["symbol"].replace("/", "").upper()
                try:
                    recent = bin_client.get_all_orders(symbol=symbol, limit=10)
                    for o in reversed(recent):
                        if str(o.get("orderListId")) != str(oco_id): continue
                        if o.get("status") == "FILLED":
                            p = float(o.get("price") or o.get("stopPrice") or 0)
                            q = float(o.get("executedQty"))
                            entry = float(meta.get("entry", 0) or 0)
                            if entry == 0: entry = p

                            if p < entry:
                                await notifier.send(client, f"🛑 SL HIT!\nSymbol: {meta['symbol']}\nPrice: ${p:.6f}\nQty: {q}")
                            else:
                                await notifier.send(client, f"🎯 TP HIT!\nSymbol: {meta['symbol']}\nPrice: ${p:.6f}\nQty: {q}")
                            
                            ts.untrack_oco(oco_id)
                            break
                except Exception: pass
            await asyncio.sleep(5)
        except Exception: await asyncio.sleep(5)

async def backend_ping_loop():
    path = os.path.join(ts.RUNTIME_DIR, "backend.ping")
    while True:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
            os.utime(path, None)
        except Exception: pass
        await asyncio.sleep(10)

# --- Main ---
async def main():
    load_dotenv()
    print("✅ Starting main()…")
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    channel = os.environ["TG_CHANNEL_ID_OR_USERNAME"]
    notify_chat = os.environ["TG_NOTIFY_CHAT_ID"]

    session_path = os.path.join(os.path.dirname(__file__), "signals_session")
    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()

    # Resolve Channel
    try:
        if channel.isdigit() or (channel.startswith("-") and channel[1:].isdigit()):
            channel_to_listen = int(channel)
        else:
            channel_to_listen = channel
    except:
        channel_to_listen = channel
    
    print(f"👂 Listening to: {channel_to_listen}")

    notifier = sv.Notifier(notify_chat)
    await notifier.send(client, "✅ Bot connected and ready!")
    await sv.cache_telegram_entities(client, channel_to_listen, notify_chat, notifier)

    cfg = ts.read_settings()
    binance = sv.BinanceSpot(
        os.environ["BINANCE_API_KEY"],
        os.environ["BINANCE_API_SECRET"],
        cfg.dry_run,
        cfg.use_testnet
    )
    binance.prefer_usdc = (cfg.quote_asset.upper() == "USDC")
    
    trader = Trader(binance, client, notifier)

    try:
        ai_parser = AISignalParser()
        print("AI PARSER: Initialized")
    except Exception as e:
        ai_parser = None
        print(f"AI PARSER: Failed {e}")

    # Start Tasks
    asyncio.create_task(audit_positions_loop(binance, notifier, client))
    asyncio.create_task(heartbeat_watchdog(notifier, client))
    asyncio.create_task(flatten_watchdog(binance, notifier, client))
    asyncio.create_task(monitor_orders_loop(binance, notifier, client))
    asyncio.create_task(monitor_tracked_oco_loop(notifier, client))
    asyncio.create_task(backend_ping_loop())

    @client.on(events.NewMessage(chats=channel_to_listen))
    async def handler(event):
        global last_signal_ts
        text = event.message.message or ""
        last_signal_ts = time.time()
        print(f"[MSG] {text[:50]}...")

        has_keywords = re.search(r'signal|إشارة|spot|coin|entry|buy|sell|trade', text, flags=re.IGNORECASE)
        sig = None

        if not has_keywords:
            if ai_parser:
                sig = ai_parser.parse(text)
                if sig: ts.emit("ai_parse_success", {"currency": sig.currency_display})
        else:
            ts.emit("new_message", {"preview": text[:100]})
            sig = parse_signal(text)
            if not sig and ai_parser:
                sig = ai_parser.parse(text)

        if not sig:
            ts.emit("ignored", {"reason": "parse_failed"})
            return

        ts.emit("parse_success", {"currency": sig.currency_display, "entry": sig.entry})
        await trader.on_signal(sig)

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())