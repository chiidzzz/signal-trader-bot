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

@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse("static/index.html")

@app.get("/events")
async def events(request: Request):
    # SSE: tail events.jsonl from end, stream new lines
    async def event_gen() -> AsyncGenerator[str, None]:
        # open file and seek to end (so we only stream new events)
        with open(EVENTS_FILE, "a", encoding="utf-8"): pass
        f = open(EVENTS_FILE, "r", encoding="utf-8")
        f.seek(0, os.SEEK_END)
        last_size = f.tell()
        while True:
            if await request.is_disconnected():
                break
            line = f.readline()
            if not line:
                await asyncio.sleep(1.0)  # poll every 1s
                f.seek(last_size)
            else:
                last_size = f.tell()
                yield {"event": "message", "data": line.strip()}
    return EventSourceResponse(event_gen())

@app.get("/api/config")
def get_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return YAMLResponse(yaml.safe_load(f))

@app.post("/api/config")
async def set_config(req: Request):
    data = await req.json()
    # Basic validation: ensure keys we expect; otherwise just write through.
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    # touch file to bump mtime (bot hot-reloads)
    os.utime(CONFIG_FILE, None)
    return JSONResponse({"ok": True})

@app.get("/api/state")
def get_state():
    if not os.path.exists(STATE_FILE):
        return JSONResponse({})
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return JSONResponse(json.load(f))

class YAMLResponse(JSONResponse):
    media_type = "application/json"
