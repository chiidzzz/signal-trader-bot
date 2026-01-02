import os
import time
import asyncio
import httpx
import json
import yaml
from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException

# --- Load environment variables (.env must contain TG_BOT_TOKEN + TG_NOTIFY_CHAT_ID) ---
load_dotenv()

# --- Constants ---
RUNTIME_DIR = "runtime"
BACKEND_PING = os.path.join(RUNTIME_DIR, "backend.ping")
FRONTEND_PING = os.path.join(RUNTIME_DIR, "frontend.ping")
STATUS_FILE = os.path.join(RUNTIME_DIR, "status.json")

CHECK_INTERVAL = 10
STALE_THRESHOLD_BACKEND = 45
STALE_THRESHOLD_FRONTEND = 90
DEBOUNCE_LIMIT = 3

# --- Binance check ---
BINANCE_CHECK_INTERVAL = 15 * 60  # every 15 minutes

# --- Telegram ---
TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_NOTIFY_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

# --- Helper: load machine name from config.yaml ---
def get_machine_name():
    try:
        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
            return cfg.get("machine_name", "Machine")
    except:
        return "Machine"

# --- Helper: update status WITHOUT emitting to event log ---
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
    key = os.getenv("BINANCE_API_KEY")
    secret = os.getenv("BINANCE_API_SECRET")

    if not key or not secret:
        return False, "Missing BINANCE_API_KEY/BINANCE_API_SECRET in .env"

    try:
        c = Client(key, secret)
        # Signed endpoint -> will fail on IP restriction (-2015)
        c.get_account()
        return True, "OK"
    except BinanceAPIException as e:
        # The exact issue you reported: -2015 Invalid API-key, IP, or permissions
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

    last_state = None
    last_net_state = None

    # --- NEW: Binance state tracking ---
    last_binance_state = None
    last_binance_check = 0

    backend_misses = 0
    frontend_misses = 0

    while True:
        name = get_machine_name()  # reload live if user changes it
        now = time.time()
        backend_alive = False
        frontend_alive = False

        # --- Backend ping ---
        try:
            age_b = now - os.path.getmtime(BACKEND_PING)
            backend_alive = age_b < STALE_THRESHOLD_BACKEND
            if not backend_alive:
                print(f"[WATCHDOG] {name} Backend ping stale: {age_b:.1f}s old")
        except FileNotFoundError:
            print(f"[WATCHDOG] {name} Backend ping file not found")

        # --- Frontend ping: only alert if UI was active recently ---
        FRONTEND_ACTIVE_WINDOW = 5 * 60      # if UI pinged within last 5 min => UI is considered "in use"
        FRONTEND_STALE_ALERT = 30            # if UI is "in use" but stops pinging for 30s => alert

        try:
            age_f = now - os.path.getmtime(FRONTEND_PING)

            if age_f > FRONTEND_ACTIVE_WINDOW:
                # UI is not being used (browser closed) -> treat as OK and stay quiet
                frontend_alive = True
            else:
                # UI was active recently -> now it must keep pinging
                frontend_alive = age_f < FRONTEND_STALE_ALERT
                if not frontend_alive:
                    print(f"[WATCHDOG] {name} Frontend ping stale: {age_f:.1f}s old")

        except FileNotFoundError:
            # UI never opened -> don't alert
            frontend_alive = True

        # --- Debouncing ---
        backend_misses = backend_misses + 1 if not backend_alive else 0
        frontend_misses = frontend_misses + 1 if not frontend_alive else 0

        # --- Determine state ---
        if backend_misses >= DEBOUNCE_LIMIT and frontend_misses >= DEBOUNCE_LIMIT:
            state = "both_down"
            msg = f"🚨 Frontend + Backend DOWN at {time.strftime('%H:%M:%S')}"
        elif backend_misses >= DEBOUNCE_LIMIT:
            state = "backend_down"
            msg = f"⚠️ Backend DOWN at {time.strftime('%H:%M:%S')}"
        elif frontend_misses >= DEBOUNCE_LIMIT:
            state = "frontend_down"
            msg = f"⚠️ Frontend DOWN at {time.strftime('%H:%M:%S')}"
        else:
            state = "ok"
            msg = f"✅ Frontend + Backend OK at {time.strftime('%H:%M:%S')}"

        # --- Update UI status ---
        update_status(f"{name}: {msg}")

        # --- Send Telegram only on state changes ---
        if state != last_state:
            await send_telegram(msg)
            last_state = state

        # --- Internet check ---
        net_ok = await check_internet()
        if net_ok and last_net_state is not True:
            await send_telegram(f"✅ Internet connection restored at {time.strftime('%H:%M:%S')}")
        elif not net_ok and last_net_state is not False:
            print(f"[Network] ⚠️ {name} Internet lost at {time.strftime('%H:%M:%S')}")

        last_net_state = net_ok

        # --- NEW: Binance auth/IP check (every 15 minutes) ---
        if time.time() - last_binance_check >= BINANCE_CHECK_INTERVAL:
            last_binance_check = time.time()
            bin_ok, bin_reason = await check_binance_auth()

            if bin_ok:
                # Send ONLY once when it comes back (no OK spam)
                if last_binance_state is not True:
                    await send_telegram("✅ Binance API access restored (IP authorized)")
                update_status(f"{name}: ✅ Binance API OK")
                last_binance_state = True
            else:
                # Send EVERY 15 minutes while blocked (your preference)
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
