#!/usr/bin/env bash
set -euo pipefail

PORT=${PORT:-8000}
UVICORN_WORKERS=${UVICORN_WORKERS:-1}

uvicorn ui_server:app --host 0.0.0.0 --port "${PORT}" --workers "${UVICORN_WORKERS}" &
UI_PID=$!

python signal_trader.py &
BOT_PID=$!

PIDS=("${UI_PID}" "${BOT_PID}")

if [ "${ENABLE_WATCHDOG:-0}" = "1" ]; then
  python watchdog.py &
  WD_PID=$!
  PIDS+=("${WD_PID}")
fi

terminate() {
  trap - SIGINT SIGTERM
  kill "${PIDS[@]}" 2>/dev/null || true
}

trap terminate SIGINT SIGTERM

wait -n "${PIDS[@]}"
terminate
wait || true
