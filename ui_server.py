# ui_server.py
import os, json, time, asyncio, yaml
from typing import AsyncGenerator
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

# -------------------------------------------------------------------
# Paths and constants
# -------------------------------------------------------------------
RUNTIME_DIR = "runtime"
EVENTS_FILE = os.path.join(RUNTIME_DIR, "events.jsonl")
STATE_FILE = os.path.join(RUNTIME_DIR, "state.json")
CONFIG_FILE = "config.yaml"

os.makedirs(RUNTIME_DIR, exist_ok=True)

# -------------------------------------------------------------------
# FastAPI setup
# -------------------------------------------------------------------
app = FastAPI(title="Signals Bot UI")
app.mount("/static", StaticFiles(directory="static"), name="static")

# -------------------------------------------------------------------
# Root page
# -------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    """Serve dashboard HTML."""
    return FileResponse("static/index.html")

# -------------------------------------------------------------------
# SSE event stream (live logs)
# -------------------------------------------------------------------
@app.get("/events")
async def events(request: Request):
    """Stream events.jsonl as Server-Sent Events (SSE)."""
    async def event_gen() -> AsyncGenerator[str, None]:
        # ensure file exists
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        with open(EVENTS_FILE, "a", encoding="utf-8"):
            pass
        f = open(EVENTS_FILE, "r", encoding="utf-8")
        f.seek(0, os.SEEK_END)
        last_size = f.tell()
        while True:
            if await request.is_disconnected():
                break
            line = f.readline()
            if not line:
                await asyncio.sleep(1.0)
                f.seek(last_size)
            else:
                last_size = f.tell()
                yield {"event": "message", "data": line.strip()}
    return EventSourceResponse(event_gen())

# -------------------------------------------------------------------
# Config helpers
# -------------------------------------------------------------------
def load_config():
    """Read YAML config."""
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def save_config(data: dict):
    """Write YAML config."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    os.utime(CONFIG_FILE, None)

# -------------------------------------------------------------------
# Config API
# -------------------------------------------------------------------
@app.get("/api/config")
def get_config():
    """Return current config.yaml contents."""
    cfg = load_config()
    return JSONResponse(cfg)

@app.post("/api/config")
async def set_config(req: Request):
    """Save config.yaml from dashboard POST."""
    data = await req.json()
    save_config(data)
    return JSONResponse({"ok": True})

# -------------------------------------------------------------------
# State API
# -------------------------------------------------------------------
@app.get("/api/state")
def get_state():
    """Return latest bot state (if available)."""
    if not os.path.exists(STATE_FILE):
        return JSONResponse({})
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        try:
            return JSONResponse(json.load(f))
        except Exception:
            return JSONResponse({"error": "invalid state file"})

# -------------------------------------------------------------------
# YAMLResponse helper (optional legacy)
# -------------------------------------------------------------------
class YAMLResponse(JSONResponse):
    media_type = "application/json"
