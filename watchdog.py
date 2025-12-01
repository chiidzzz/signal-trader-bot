import os
import time
import asyncio
import httpx
from dotenv import load_dotenv
import json


# --- Load environment variables (.env must contain TG_BOT_TOKEN + TG_NOTIFY_CHAT_ID) ---
load_dotenv()

# --- Constants ---
RUNTIME_DIR = "runtime"
BACKEND_PING = os.path.join(RUNTIME_DIR, "backend.ping")
FRONTEND_PING = os.path.join(RUNTIME_DIR, "frontend.ping")
STATUS_FILE = os.path.join(RUNTIME_DIR, "status.json")  # New: separate status file

CHECK_INTERVAL = 10        # seconds between checks
STALE_THRESHOLD_BACKEND = 45   # backend should ping reliably every 10s
STALE_THRESHOLD_FRONTEND = 90  # frontend can miss pings due to browser sleep/network
DEBOUNCE_LIMIT = 3         # how many consecutive misses before declaring DOWN

# --- Telegram credentials ---
TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = os.getenv("TG_NOTIFY_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

# --- Helper: update status WITHOUT emitting to event log ---
def update_status(msg: str):
    """Update status display without cluttering event log."""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    status = {
        "ts": int(time.time()),
        "msg": msg,
        "is_down": "DOWN" in msg or "🚨" in msg or "⚠️" in msg
    }
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

# --- Helper: send Telegram message ---
async def send_telegram(text: str):
    if not (TOKEN and CHAT_ID):
        print("⚠️ Telegram not configured.")
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
            if r.status_code == 200:
                print(f"📤 Telegram OK: {text}")
                return True
            else:
                print(f"❌ Telegram API error {r.status_code}: {r.text}")
                return False
    except Exception as e:
        print(f"❌ Telegram send failed: {e}")
        return False

# --- Network connectivity check ---
async def check_internet():
    """Return True if at least one target is reachable."""
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
            except Exception:
                continue
    return False

# --- Main watchdog loop ---
async def monitor():
    print("🕵️ MSI Laptop Watchdog started…")
    await send_telegram("🟢 MSI Laptop Watchdog started and monitoring backend/frontend health")

    last_state = None  # "ok", "backend_down", "frontend_down", "both_down"
    last_net_state = None  # internet connectivity state
    frontend_misses = 0
    backend_misses = 0

    while True:
        now = time.time()
        backend_alive = False
        frontend_alive = False

        # === Check backend ping file ===
        try:
            mtime_b = os.path.getmtime(BACKEND_PING)
            age_b = now - mtime_b
            backend_alive = age_b < STALE_THRESHOLD_BACKEND
            if not backend_alive:
                print(f"[WATCHDOG] MSI Laptop Backend ping stale: {age_b:.1f}s old (threshold: {STALE_THRESHOLD_BACKEND}s)")
        except FileNotFoundError:
            print(f"[WATCHDOG] MSI Laptop Backend ping file not found")
            pass

        # === Check frontend ping file ===
        try:
            mtime_f = os.path.getmtime(FRONTEND_PING)
            age_f = now - mtime_f
            frontend_alive = age_f < STALE_THRESHOLD_FRONTEND
            if not frontend_alive:
                print(f"[WATCHDOG] MSI Laptop Frontend ping stale: {age_f:.1f}s old (threshold: {STALE_THRESHOLD_FRONTEND}s)")
        except FileNotFoundError:
            print(f"[WATCHDOG] MSI Laptop Frontend ping file not found")
            pass

        # === Debounce logic ===
        if not backend_alive:
            backend_misses += 1
        else:
            if backend_misses > 0:
                print(f"[WATCHDOG] MSI Laptop Backend recovered (was down for {backend_misses} checks)")
            backend_misses = 0

        if not frontend_alive:
            frontend_misses += 1
        else:
            if frontend_misses > 0:
                print(f"[WATCHDOG] MSI Laptop Frontend recovered (was down for {frontend_misses} checks)")
            frontend_misses = 0

        # === Determine combined state ===
        if backend_misses >= DEBOUNCE_LIMIT and frontend_misses >= DEBOUNCE_LIMIT:
            state = "both_down"
            msg = f"🚨 MSI Laptop Frontend + Backend DOWN at {time.strftime('%H:%M:%S')}"
        elif backend_misses >= DEBOUNCE_LIMIT:
            state = "backend_down"
            msg = f"⚠️ MSI Laptop Backend DOWN at {time.strftime('%H:%M:%S')}"
        elif frontend_misses >= DEBOUNCE_LIMIT:
            state = "frontend_down"
            msg = f"⚠️ MSI Laptop Frontend DOWN at {time.strftime('%H:%M:%S')}"
        else:
            state = "ok"
            msg = f"✅ MSI Laptop Frontend + Backend OK at {time.strftime('%H:%M:%S')}"

        # === Always update status display (no event log spam) ===
        update_status(msg)

        # === Send Telegram only when status changes ===
        if state != last_state:
            success = await send_telegram(msg)
            if success:
                last_state = state
            else:
                print("[WATCHDOG] MSI Laptop Telegram failed, will retry next loop")


        # === Network check (Internet/API reachability) ===
        net_ok = await check_internet()
        if net_ok and last_net_state is not True:
            await send_telegram(f"✅ MSI Laptop Internet connection restored at {time.strftime('%H:%M:%S')}")
        elif not net_ok and last_net_state is not False:
            print(f"[Network] ⚠️ MSI Laptop Internet lost at {time.strftime('%H:%M:%S')}")
            # Can't send Telegram here if net is down
        last_net_state = net_ok

        await asyncio.sleep(CHECK_INTERVAL)

# --- Entrypoint ---
if __name__ == "__main__":
    asyncio.run(monitor())