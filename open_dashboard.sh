#!/bin/zsh
# Start the agent if it isn't running, then open the dashboard.
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=$(grep -E '^DASHBOARD_PORT=' "$DIR/.env" 2>/dev/null | cut -d= -f2)
PORT=${PORT:-8765}
URL="http://localhost:$PORT"
if ! curl -s -o /dev/null "$URL/api/data"; then
  cd "$DIR" && nohup .venv/bin/python -u jobmail.py --no-browser >> jobmail.log 2>&1 &
  for i in {1..30}; do curl -s -o /dev/null "$URL/api/data" && break; sleep 0.5; done
fi
open "$URL"
