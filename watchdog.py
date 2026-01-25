import os
import time
import asyncio
import httpx
import json
import yaml
from dotenv import load_dotenv
from binance.exceptions import BinanceAPIException
# 1. IMPORT FACTORY (Ensures time sync)
from services import get_synced_client 

# --- Load environment variables ---
load_dotenv()

# --- Constants ---
RUNTIME_DIR = "runtime"
BACKEND_PING = os.path.join(RUNTIME_DIR, "backend.ping")
FRONTEND_PING = os.path.join(RUNTIME_DIR, "frontend.ping")
UI_SERVER_PING = os.path.join(RUNTIME_DIR, "ui_server.ping")
UI_SERVER_ACTIVE_WINDOW_SEC = 20  
STATUS_FILE = os.path.join(RUNTIME_DIR, "status.json")
CHECK_INTERVAL = 10
STALE_THRESHOLD_BACKEND = 45
STALE_THRESHOLD_FRONTEND = 90
DEBOUNCE_LIMIT = 5
# UI open/close detection
UI_ACTIVE_WINDOW_SEC = 60
UI_MIN_OPEN_SEC = 30
UI_CLOSE_DEBOUNCE = 3
UI_OPEN_DEBOUNCE  = 1
# --- Binance check ---
BINANCE_CHECK_INTERVAL = 15 * 60  # every 15 minutes
# --- Telegram ---
TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_NOTIFY_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

# --- Helper: load machine name ---
def get_machine_name():
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
            return cfg.get("machine_name", "Machine")
    except:
        return "Machine"

# --- Helper: update status ---
def update_status(msg: str):
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    status = {
        "ts": int(time.time()),
        "msg": msg,
        "is_down": any(x in msg for x in ["DOWN", "🚨", "⚠️", "BLOCKED"])
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

# --- Helper: send telegram ---
async def send_telegram(text: str):
    if not (TOKEN and CHAT_ID):
        print("⚠️ Telegram not configured.")
        return False

    name = get_machine_name()
    final = f"💻 *{name}* — {text}"

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": final, "parse_mode": "Markdown"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            if r.status_code == 200:
                print(f"📤 Telegram OK: {final}")
                return True
            else:
                print(f"❌ Telegram API error {r.status_code}: {r.text}")
                return False
    except Exception as e:
        print(f"❌ Telegram send failed: {e}")
        return False

# --- Check internet ---
async def check_internet():
    TEST_URLS = [
        "https://api.binance.com/api/v3/ping",
        "https://www.google.com",
        "https://www.cloudflare.com",
    ]
    async with httpx.AsyncClient(timeout=5) as client:
        for url in TEST_URLS:
            try:
                await client.get(url)
                return True
            except:
                continue
    return False

# --- NEW: Check Binance AUTH (signed endpoint) ---
async def check_binance_auth():
    """
    Returns:
      (True, "OK") if Binance signed endpoint is reachable (auth/IP OK)
      (False, reason_str) otherwise
    """
    try:
        # 2. USE FACTORY (Automatically handles API keys & Time Sync)
        c = get_synced_client()
        
        # 3. CALL WITH recvWindow (Prevents -1021 errors)
        c.get_account(recvWindow=60000)
        return True, "OK"
    except BinanceAPIException as e:
        if getattr(e, "code", None) == -2015:
            return False, f"Binance blocked (IP/API permission): {e.message}"
        return False, f"Binance API exception ({getattr(e,'code',None)}): {str(e)}"
    except Exception as e:
        return False, f"Binance unexpected error: {e}"

# --- Main watchdog ---
async def monitor():
    name = get_machine_name()
    print(f"🕵️ {name} Watchdog started…")
    await send_telegram("🟢 Watchdog started and monitoring backend/frontend health")

    last_net_state = None
    last_backend_state = None
    last_ui_state = False
    last_ui_server_state = None 
    ui_open_since = None
    last_binance_state = None
    last_binance_check = 0
    backend_misses = 0
    ui_misses = 0
    ui_hits = 0

    while True:
        name = get_machine_name()
        now = time.time()
        backend_alive = False

        # --- Backend ping ---
        try:
            age_b = now - os.path.getmtime(BACKEND_PING)
            backend_alive = age_b < STALE_THRESHOLD_BACKEND
            if not backend_alive:
                print(f"[WATCHDOG] {name} Backend ping stale: {age_b:.1f}s old")
        except FileNotFoundError:
            print(f"[WATCHDOG] {name} Backend ping file not found")
        
        backend_misses = backend_misses + 1 if not backend_alive else 0
        
        # --- Determine state and alert ONLY on changes ---
        if backend_misses >= DEBOUNCE_LIMIT:
            state = "backend_down"
            msg = f"❌⛔🚨 Backend DOWN at {time.strftime('%H:%M:%S')}"
        else:
            state = "backend_ok"
            msg = f"✅ Backend OK at {time.strftime('%H:%M:%S')}"
        
        # --- UI SERVER up/down ---
        try:
            age_us = now - os.path.getmtime(UI_SERVER_PING)
            ui_server_alive = age_us <= UI_SERVER_ACTIVE_WINDOW_SEC
        except FileNotFoundError:
            ui_server_alive = False

        ui_server_state = "up" if ui_server_alive else "down"
        if ui_server_state != last_ui_server_state:
            if ui_server_alive:
                await send_telegram(f"🟢 UI server UP at {time.strftime('%H:%M:%S')}")
            else:
                await send_telegram(f"🔴 UI server DOWN at {time.strftime('%H:%M:%S')}")
            last_ui_server_state = ui_server_state

        # --- UI open/close ---
        if not ui_server_alive:
            ui_hits = 0
            ui_misses = 0
            await asyncio.sleep(CHECK_INTERVAL)
            continue
        try:
            age_f = now - os.path.getmtime(FRONTEND_PING)
            frontend_alive_now = age_f <= UI_ACTIVE_WINDOW_SEC
        except FileNotFoundError:
            frontend_alive_now = False

        if frontend_alive_now:
            ui_hits += 1
            ui_misses = 0
        else:
            ui_misses += 1
            ui_hits = 0

        if (not last_ui_state) and ui_hits >= UI_OPEN_DEBOUNCE:
            last_ui_state = True
            ui_open_since = now
            await send_telegram(f"🟢 UI opened at {time.strftime('%H:%M:%S')}")

        if last_ui_state and ui_misses >= UI_CLOSE_DEBOUNCE:
            last_ui_state = False
            open_dur = (now - ui_open_since) if ui_open_since else 0
            if open_dur >= UI_MIN_OPEN_SEC:
                await send_telegram(
                    f"🟡 UI closed at {time.strftime('%H:%M:%S')} (open {int(open_dur)}s)"
                )
            ui_open_since = None

        # --- Backend status text formatting ---
        if backend_misses >= DEBOUNCE_LIMIT:
            state = "backend_down"
            msg = f"❌⛔🚨 Backend DOWN at {time.strftime('%H:%M:%S')}"
        else:
            state = "backend_ok"
            try:
                bt = os.path.getmtime(BACKEND_PING)
                msg = f"✅ Backend OK at {time.strftime('%H:%M:%S', time.localtime(bt))}"
            except FileNotFoundError:
                msg = f"✅ Backend OK at {time.strftime('%H:%M:%S')}"

        update_status(f"{name} — {msg}")

        if state != last_backend_state:
            await send_telegram(msg)
            last_backend_state = state

        # --- Internet check ---
        net_ok = await check_internet()
        if net_ok and last_net_state is not True:
            await send_telegram(f"✅ Internet connection restored at {time.strftime('%H:%M:%S')}")
        elif not net_ok and last_net_state is not False:
            print(f"[Network] ⚠️ {name} Internet lost at {time.strftime('%H:%M:%S')}")

        last_net_state = net_ok

        # --- NEW: Binance auth/IP check ---
        if time.time() - last_binance_check >= BINANCE_CHECK_INTERVAL:
            last_binance_check = time.time()
            bin_ok, bin_reason = await check_binance_auth()

            if bin_ok:
                if last_binance_state is not True:
                    await send_telegram("✅ Binance API access restored (IP authorized)")
                update_status(f"{name}: ✅ Binance API OK")
                last_binance_state = True
            else:
                await send_telegram(
                    "🚨 Binance API BLOCKED!\n"
                    "IP changed or not whitelisted.\n"
                    f"Reason: {bin_reason}\n"
                    "⚠️ Trading will FAIL until fixed."
                )
                update_status(f"{name}: 🚨 Binance API BLOCKED (IP issue)")
                last_binance_state = False

        await asyncio.sleep(CHECK_INTERVAL)

# --- Entrypoint ---
if __name__ == "__main__":
    asyncio.run(monitor())