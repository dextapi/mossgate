#!/bin/sh
# Start the gateway detached on 127.0.0.1:8787 (override HOST/PORT to expose).
cd "$(dirname "$0")" || exit 1
PORT="${PORT:-8787}"
PY="${PY:-.venv/bin/python}"
[ -x "$PY" ] || PY=python3
if curl -fsS "http://127.0.0.1:$PORT/_health" >/dev/null 2>&1; then echo "already running on $PORT"; exit 0; fi
nohup "$PY" run.py > gateway.log 2>&1 &
echo $! > gateway.pid
sleep 2
curl -fsS "http://127.0.0.1:$PORT/_health" >/dev/null && echo "up on http://127.0.0.1:$PORT (pid $(cat gateway.pid))" || { echo "failed, see gateway.log"; exit 1; }
