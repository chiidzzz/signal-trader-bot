import os, json, time, asyncio, yaml, importlib
from typing import AsyncGenerator
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from dotenv import load_dotenv


# --- load .env automatically ---
load_dotenv()

RUNTIME_DIR = "runtime"
EVENTS_FILE = os.path.join(RUNTIME_DIR, "events.jsonl")
STATE_FILE = os.path.join(RUNTIME_DIR, "state.json")
STATUS_FILE = os.path.join(RUNTIME_DIR, "status.json")  # New: separate status file
CONFIG_FILE = "config.yaml"

os.makedirs(RUNTIME_DIR, exist_ok=True)

app = FastAPI(title="Signals Bot UI")
app.mount("/static", StaticFiles(directory="static"), name="static")


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