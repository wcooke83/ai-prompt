#!/bin/bash
#
# Start (or restart) the standalone ai-prompt HTTP bridge.
#
# This is the HEADLESS / FALLBACK way to run the bridge: a single host process
# that serves the REST API on $PORT (POST /prompt, /status, /sessions, /health)
# and a WebSocket on $WS_PORT that the browser extension connects to as a client.
# It is NOT in Docker and has NO hot-reload, so restart it after editing the
# bridge's Python (e.g. adding a provider to the Provider enum).
#
# You usually do NOT need this script. The recommended setup lets the browser
# launch and manage the bridge automatically via native messaging — see
# scripts/install-native-host.sh and the extension popup's "HTTP API Port"
# field. Use this script only when you want the bridge without the browser
# managing it (headless automation, CI, or the legacy WebSocket transport).
#
# Usage:
#   ./scripts/start-bridge.sh            # (re)start the bridge, wait until ready
#   ./scripts/start-bridge.sh --status   # show bridge status, leave it running
#   ./scripts/start-bridge.sh --stop     # stop the bridge
#   ./scripts/start-bridge.sh --help
#
# Env overrides:
#   AI_PROMPT_DIR   path to the ai-prompt repo         (default: this script's repo)
#   PORT            bridge HTTP port                    (default: 8200)
#   WS_PORT         bridge WebSocket port (extension)   (default: 8760)
#
# Running MULTIPLE bridges on one machine — each instance needs its own PORT and
# WS_PORT, plus its own browser profile with the extension's WebSocket Port set
# to match that instance's WS_PORT:
#   PORT=8201 WS_PORT=8761 ./scripts/start-bridge.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_PROMPT_DIR="${AI_PROMPT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PORT="${PORT:-8200}"
WS_PORT="${WS_PORT:-8760}"
UVICORN="${AI_PROMPT_DIR}/ai_prompt_api/.venv/bin/uvicorn"
LOG="/tmp/ai-prompt-bridge-${PORT}.log"

# Print the header comment block (everything after the shebang up to the first non-# line).
usage() { awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; }

status() { curl -s --max-time 5 "http://localhost:${PORT}/status" 2>/dev/null || true; }

stop_bridge() {
    local pids
    # -sTCP:LISTEN is load-bearing: a bare `lsof -i tcp:PORT` also matches processes
    # holding an ESTABLISHED connection *to* that port. Once the router is running
    # beside the bridges it keeps keep-alive sockets open to every bridge port, so
    # without this filter `--stop` would take the router down as collateral (and
    # vice versa for any app connected to the router).
    pids=$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        echo "Stopping bridge on :${PORT} (pid $(echo "$pids" | tr '\n' ' '))..."
        echo "$pids" | xargs kill 2>/dev/null || true
    else
        echo "Nothing listening on :${PORT}."
    fi
}

case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    --status)
        s=$(status)
        [[ -n "$s" ]] && echo "$s" || echo "Bridge not running on :${PORT}."
        exit 0 ;;
    --stop) stop_bridge; exit 0 ;;
    "") ;;
    *) echo "Unknown option: $1"; echo; usage; exit 1 ;;
esac

if [[ ! -x "$UVICORN" ]]; then
    echo "ERROR: uvicorn not found at $UVICORN"
    echo "Is the ai-prompt repo checked out at AI_PROMPT_DIR ($AI_PROMPT_DIR) with its venv installed?"
    exit 1
fi

stop_bridge

echo "Starting bridge from ${AI_PROMPT_DIR} on :${PORT} (+ extension WS :${WS_PORT})..."
AI_PROMPT_PORT="${PORT}" AI_PROMPT_WS_PORT="${WS_PORT}" nohup "$UVICORN" ai_prompt_api.main:app \
    --app-dir "$AI_PROMPT_DIR" --host 0.0.0.0 --port "$PORT" \
    > "$LOG" 2>&1 &
disown 2>/dev/null || true

echo -n "Waiting for bridge to come up"
for _ in $(seq 1 20); do
    s=$(status)
    if [[ -n "$s" ]]; then
        echo " — up."
        echo "$s"
        echo ""
        echo "Log: $LOG"
        case "$s" in
            *'"extension_connected":true'*) ;;
            *) echo "extension_connected is false — open the browser profile with the extension so it reconnects (ws :${WS_PORT})." ;;
        esac
        exit 0
    fi
    sleep 1
done

echo ""
echo "ERROR: bridge did not respond on :${PORT} within 20s. Check $LOG"
exit 1
