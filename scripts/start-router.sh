#!/bin/bash
#
# Start (or restart) the ai-prompt router.
#
# The router exposes the SAME HTTP contract as a bridge (POST /prompt,
# GET/DELETE /sessions[/{id}], /status, /health) and round-robins each call to
# one of the bridges listed in its config. Apps change only host/port/key; the
# router adds `X-AI-Prompt-Upstream: <name>` to every response so you can see
# where a prompt actually went. It also serves GET /upstreams (needs a key) —
# the operator dashboard: per-upstream health, in-flight count, sessions owned.
#
# The router needs NO browser of its own. It talks HTTP to the bridges, which
# are the things that need browsers. Start it AFTER the bridges if you can;
# it doesn't matter much, the health poller picks them up when they appear.
#
# Like the bridge, this is not in Docker and has NO hot-reload: restart after
# editing the router's Python OR its config file (router.json).
#
# Usage:
#   ./scripts/start-router.sh            # (re)start the router, wait until ready
#   ./scripts/start-router.sh --status   # show router status, leave it running
#   ./scripts/start-router.sh --stop     # stop the router
#   ./scripts/start-router.sh --help
#
# Env overrides:
#   AI_PROMPT_DIR               path to the ai-prompt repo   (default: this script's repo)
#   PORT                        router HTTP port             (default: 8300)
#   AI_PROMPT_ROUTER_CONFIG     router.json path             (default: ~/.ai-prompt/router.json)
#   AI_PROMPT_ROUTER_KEY        an api_keys[].key, only used by --status/the readiness
#                               probe if you have put /status behind auth (never printed,
#                               and passed via curl's stdin so it stays out of `ps`)
#
# First run on a new box:
#   python3 -m venv ai_prompt_router/.venv
#   ai_prompt_router/.venv/bin/pip install -r ai_prompt_router/requirements.txt
#   mkdir -p ~/.ai-prompt && cp ai_prompt_router/router.example.json ~/.ai-prompt/router.json
#   chmod 600 ~/.ai-prompt/router.json      # it holds the upstream bridges' keys
#   $EDITOR ~/.ai-prompt/router.json        # api_keys[] + upstreams[]
#
# On a server, prefer the systemd unit (scripts/ai-prompt-router.service) so the
# router comes back after a reboot. Use one or the other, not both — they bind
# the same port, and whichever loses the race dies.
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AI_PROMPT_DIR="${AI_PROMPT_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PORT="${PORT:-8300}"
CONFIG="${AI_PROMPT_ROUTER_CONFIG:-$HOME/.ai-prompt/router.json}"
PY="${AI_PROMPT_DIR}/ai_prompt_router/.venv/bin/python"
LOG="/tmp/ai-prompt-router-${PORT}.log"
STATUS_URL="http://localhost:${PORT}/status"

# Print the header comment block (everything after the shebang up to the first non-# line).
usage() { awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; }

# curl /status with any extra args. The key (when set) goes in through curl's
# stdin config so it never lands in the process list on a shared box.
curl_status() {
    if [[ -n "${AI_PROMPT_ROUTER_KEY:-}" ]]; then
        printf 'header = "Authorization: Bearer %s"\n' "$AI_PROMPT_ROUTER_KEY" \
            | curl -s --max-time 5 -K - "$@" "$STATUS_URL" 2>/dev/null || true
    else
        curl -s --max-time 5 "$@" "$STATUS_URL" 2>/dev/null || true
    fi
}

status() { curl_status; }

# 200 = up, 401 = up but our probe has no key, 000 = nothing listening.
status_code() { curl_status -o /dev/null -w '%{http_code}'; }

stop_router() {
    local pids
    # -sTCP:LISTEN is load-bearing: a bare `lsof -i tcp:PORT` also matches processes
    # holding an ESTABLISHED connection *to* that port. Once the router is running
    # beside the bridges it keeps keep-alive sockets open to every bridge port, so
    # without this filter `--stop` would take the router down as collateral (and
    # vice versa for any app connected to the router).
    pids=$(lsof -ti "tcp:${PORT}" -sTCP:LISTEN 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        echo "Stopping router on :${PORT} (pid $(echo "$pids" | tr '\n' ' '))..."
        echo "$pids" | xargs kill 2>/dev/null || true
    else
        echo "Nothing listening on :${PORT}."
    fi
}

case "${1:-}" in
    --help|-h) usage; exit 0 ;;
    --status)
        case "$(status_code)" in
            200) status; echo "" ;;
            401) echo "Router is up on :${PORT} but /status wants a key — set AI_PROMPT_ROUTER_KEY." ;;
            *)   echo "Router not running on :${PORT}." ;;
        esac
        exit 0 ;;
    --stop) stop_router; exit 0 ;;
    "") ;;
    *) echo "Unknown option: $1"; echo; usage; exit 1 ;;
esac

if [[ ! -x "$PY" ]]; then
    echo "ERROR: router venv python not found at $PY"
    echo "Is the ai-prompt repo checked out at AI_PROMPT_DIR ($AI_PROMPT_DIR) with the router venv installed?"
    echo "  python3 -m venv '${AI_PROMPT_DIR}/ai_prompt_router/.venv'"
    echo "  '${AI_PROMPT_DIR}/ai_prompt_router/.venv/bin/pip' install -r '${AI_PROMPT_DIR}/ai_prompt_router/requirements.txt'"
    exit 1
fi

# Warn before we stop anything: a missing config is the usual reason a restart
# comes back dead, and it is much easier to read here than in the log.
if [[ ! -f "$CONFIG" ]]; then
    echo "WARNING: no router config at $CONFIG"
    echo "  cp '${AI_PROMPT_DIR}/ai_prompt_router/router.example.json' '$CONFIG' && chmod 600 '$CONFIG'"
    echo "Starting anyway — if the router exits immediately, this is why."
fi

stop_router

echo "Starting router from ${AI_PROMPT_DIR} on :${PORT} (config: ${CONFIG})..."
nohup env PYTHONPATH="$AI_PROMPT_DIR" \
    AI_PROMPT_ROUTER_CONFIG="$CONFIG" \
    AI_PROMPT_ROUTER_PORT="$PORT" \
    "$PY" -m ai_prompt_router \
    > "$LOG" 2>&1 &
disown 2>/dev/null || true

echo -n "Waiting for router to come up"
for _ in $(seq 1 20); do
    code=$(status_code)
    case "$code" in
        200)
            s=$(status)
            echo " — up."
            echo "$s"
            echo ""
            echo "Log: $LOG"
            case "$s" in
                *'"extension_connected":true'*) ;;
                *) echo "extension_connected is false — no upstream bridge is healthy yet. Check the bridges in ${CONFIG} are running, then: curl -H 'Authorization: Bearer <key>' http://localhost:${PORT}/upstreams" ;;
            esac
            exit 0 ;;
        401)
            echo " — up (/status wants a key; set AI_PROMPT_ROUTER_KEY to see the body)."
            echo "Log: $LOG"
            exit 0 ;;
    esac
    sleep 1
done

echo ""
echo "ERROR: router did not respond on :${PORT} within 20s. Check $LOG"
exit 1
