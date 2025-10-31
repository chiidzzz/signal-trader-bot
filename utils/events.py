# utils/events.py
import os, json, time

RUNTIME_DIR = "runtime"
EVENTS_FILE = os.path.join(RUNTIME_DIR, "events.jsonl")
os.makedirs(RUNTIME_DIR, exist_ok=True)

def emit(event_type: str, payload: dict):
    """Centralized event writer for dashboard."""
    line = {"ts": int(time.time()), "type": event_type, **payload}
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
