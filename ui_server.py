# ui_server.py
import os, json, time, asyncio, yaml
from typing import AsyncGenerator
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

RUNTIME_DIR = "runtime"
EVENTS_FILE = os.path.join(RUNTIME_DIR, "events.jsonl")
STATE_FILE = os.path.join(RUNTIME_DIR, "state.json")
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

# Optional: simple health for debugging
@app.get("/api/health")
def health():
    return JSONResponse({"ok": True, "ts": time.time()})

# -------------------- SSE (resilient) --------------------
@app.get("/events")
async def events(request: Request):
    async def event_gen() -> AsyncGenerator[str, None]:
        # ensure file exists
        with open(EVENTS_FILE, "a", encoding="utf-8"):
            pass
        try:
            f = open(EVENTS_FILE, "r", encoding="utf-8")
        except Exception:
            # if file cannot be opened, still keep the connection alive
            last_ka = time.time()
            while not await request.is_disconnected():
                if time.time() - last_ka > 10:
                    yield {"event": "message", "data": json.dumps({"ts": int(time.time()), "type": "keepalive"})}
                    last_ka = time.time()
                await asyncio.sleep(1.0)
            return

        f.seek(0, os.SEEK_END)
        last_keepalive = time.time()
        while not await request.is_disconnected():
            line = f.readline()
            if line:
                yield {"event": "message", "data": line.strip()}
            else:
                # periodic keepalive (avoid ERR_INCOMPLETE_CHUNKED_ENCODING)
                if time.time() - last_keepalive > 10:
                    yield {"event": "message", "data": json.dumps({"ts": int(time.time()), "type": "keepalive"})}
                    last_keepalive = time.time()
                await asyncio.sleep(1.0)
        try:
            f.close()
        except Exception:
            pass

    return EventSourceResponse(event_gen())
