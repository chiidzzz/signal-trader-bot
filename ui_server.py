import os, json, time, asyncio, yaml, importlib
from typing import AsyncGenerator
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from dotenv import load_dotenv
import subprocess
from contextlib import asynccontextmanager

# --- load .env automatically ---
load_dotenv()

RUNTIME_DIR = "runtime"
EVENTS_FILE = os.path.join(RUNTIME_DIR, "events.jsonl")
STATE_FILE = os.path.join(RUNTIME_DIR, "state.json")
STATUS_FILE = os.path.join(RUNTIME_DIR, "status.json")  # New: separate status file
CONFIG_FILE = "config.yaml"

os.makedirs(RUNTIME_DIR, exist_ok=True)

@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_evt = asyncio.Event()
    task = asyncio.create_task(_ui_server_heartbeat(stop_evt))
    try:
        yield
    finally:
        stop_evt.set()
        try:
            await task
        except Exception:
            pass

app = FastAPI(title="Signals Bot UI", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

UI_SERVER_PING = os.path.join(RUNTIME_DIR, "ui_server.ping")

async def _ui_server_heartbeat(stop_evt: asyncio.Event):
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    while not stop_evt.is_set():
        try:
            with open(UI_SERVER_PING, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
            os.utime(UI_SERVER_PING, None)
        except Exception as e:
            print(f"⚠️ Failed to update ui_server.ping: {e}")
        await asyncio.sleep(5)

# -------------------- helpers --------------------
def load_config_dict() -> dict:
    try:
        if not os.path.exists(CONFIG_FILE):
            return {}
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def deep_merge(a: dict, b: dict) -> dict:
    """Merge b into a (in place) preserving nested sections."""
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            deep_merge(a[k], v)
        else:
            a[k] = v
    return a


def save_config_dict(new_data: dict):
    current = load_config_dict()
    merged = deep_merge(current, new_data)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(merged, f, sort_keys=False, allow_unicode=True)
    os.utime(CONFIG_FILE, None)


# -------------------- routes --------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse("static/index.html")


@app.get("/api/config")
def get_config():
    return JSONResponse(load_config_dict())


@app.post("/api/config")
async def set_config(req: Request):
    data = await req.json()
    save_config_dict(data)
    return JSONResponse({"ok": True})


@app.get("/api/state")
def get_state():
    if not os.path.exists(STATE_FILE):
        return JSONResponse({})
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return JSONResponse(json.load(f))


@app.get("/api/health")
def health():
    return JSONResponse({"ok": True, "ts": time.time()})


@app.post("/api/ping")
async def ping():
    """Frontend heartbeat – updates a file so backend knows UI is alive."""
    FRONTEND_PING = os.path.join(RUNTIME_DIR, "frontend.ping")
    try:
        with open(FRONTEND_PING, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        os.utime(FRONTEND_PING, None)
    except Exception as e:
        print(f"⚠️ Failed to update frontend.ping: {e}")
    return JSONResponse({"ok": True, "ts": time.time()})



@app.get("/api/telegram-config")
def get_telegram_config():
    """Return Telegram configuration from environment variables and cached entity info."""
    source_id = os.getenv("TG_CHANNEL_ID_OR_USERNAME", "Not configured")
    dest_id = os.getenv("TG_NOTIFY_CHAT_ID", "Not configured")

    # Try to load cached entity names from runtime
    entity_cache_file = os.path.join(RUNTIME_DIR, "telegram_entities.json")
    entity_names = {}

    try:
        if os.path.exists(entity_cache_file):
            with open(entity_cache_file, "r", encoding="utf-8") as f:
                entity_names = json.load(f)
    except Exception as e:
        print(f"⚠️ Could not load entity cache: {e}")

    return JSONResponse({
        "source": {
            "id": source_id,
            "name": entity_names.get("source", "Unknown")
        },
        "destination": {
            "id": dest_id,
            "name": entity_names.get("destination", "Unknown")
        }
    })

@app.get("/api/bot-heartbeat")
def bot_heartbeat():
    path = os.path.join(RUNTIME_DIR, "backend.ping")
    if not os.path.exists(path):
        return JSONResponse({"ok": False, "reason": "backend.ping missing"})
    age = time.time() - os.path.getmtime(path)
    return JSONResponse({
        "ok": age < 45,          # matches watchdog threshold
        "age_sec": round(age, 2)
    })

@app.api_route("/api/binance-test", methods=["GET", "POST"])
def run_binance_test():
    # hard-timeout so it never hangs the API
    cmd = ["python3", "tests/test_binance_live.py"]
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            cwd=os.getcwd(),
        )
        return JSONResponse({
            "ok": p.returncode == 0,
            "returncode": p.returncode,
            "stdout": p.stdout[-4000:],  # trim
            "stderr": p.stderr[-4000:],  # trim
        })
    except subprocess.TimeoutExpired:
        return JSONResponse({"ok": False, "error": "timeout"}, status_code=504)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

# -------------------- SSE (status + Telegram alerts) --------------------
@app.get("/events")
async def events(request: Request):
    last_status_ts = 0

    async def event_gen():
        nonlocal last_status_ts
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        with open(EVENTS_FILE, "a", encoding="utf-8"):
            pass

        try:
            with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                f.seek(0, os.SEEK_END)
                while not await request.is_disconnected():
                    # Send status updates from separate status file (check every cycle)
                    if os.path.exists(STATUS_FILE):
                        try:
                            status_mtime = os.path.getmtime(STATUS_FILE)
                            if status_mtime > last_status_ts:
                                with open(STATUS_FILE, "r", encoding="utf-8") as sf:
                                    status = json.load(sf)
                                    last_status_ts = status_mtime
                                    status_event = {
                                        "ts": status.get("ts", int(time.time())),
                                        "type": "status_text",
                                        "msg": status.get("msg", "Status unknown")
                                    }
                                    yield {"event": "message", "data": json.dumps(status_event)}
                        except Exception as e:
                            print(f"⚠️ Status read error: {e}")

                    # Send regular event log messages
                    line = f.readline()
                    if line:
                        try:
                            # Validate that it's valid JSON before sending
                            json.loads(line)
                            yield {"event": "message", "data": line.strip()}
                        except json.JSONDecodeError:
                            print(f"⚠️ Skipping malformed line in events.jsonl: {line.strip()[:100]}")
                    
                    await asyncio.sleep(1.0)
        except Exception as e:
            print(f"❌ Error in event_gen: {e}")

    return EventSourceResponse(event_gen())