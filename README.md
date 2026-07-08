# AI Prompt

Send prompts to AI chat providers (ChatGPT, Grok, Claude, DeepSeek) programmatically. A REST API accepts prompt requests and forwards them to a Firefox browser extension that automates the provider's web interface, extracts the response, and returns it.

## Architecture

Two ways to run the HTTP bridge:

**Recommended — browser-managed (native host serves HTTP).** The browser launches a
native-messaging host automatically when the extension loads. That one process talks
to the extension over stdio **and** serves the REST API on a port the **extension
chooses** (popup → *HTTP API Port*) and pushes to it. Nothing to start by hand, nothing
to forget after a reboot, and each browser profile can use its own port.

```
HTTP Client (app)
      │  POST /prompt on the extension-chosen port
      ▼
  native host (ai_prompt_api.native_host)  ◄── stdio ──►  Extension  ◄──►  AI Provider Tab
  launched by the browser                    (native msg)  (content scripts)  (ChatGPT, Grok, …)
```

**Alternative — standalone bridge (WebSocket).** A separately-started FastAPI process
serves HTTP and runs a WebSocket server the extension dials into as a client. Use this
headless (CI, no native-messaging host) or for the legacy transport.

```
HTTP Client  ──►  REST API (FastAPI)  ◄── WebSocket ──►  Extension  ◄──►  AI Provider Tab
                  :8200                    :8760
```

Components:

1. **API** (`ai_prompt_api/`) — FastAPI routers for prompts, sessions, health. `native_host.py`
   is the browser-managed host (native messaging + HTTP); `main.py` is the standalone WebSocket bridge.
2. **Extension** (`ai-prompt-extension/`) — receives prompts via native messaging (preferred) or
   WebSocket, opens AI provider tabs, injects text, waits for responses, sends them back.
3. **CLI** (`ai-prompt-cli/`) — command-line client that talks to the extension directly (unix
   socket via native messaging, or WebSocket) for quick scripting without an app.

## Quick Start (recommended: browser-managed)

### 1. Install the native-messaging host (once)

```bash
./ai-prompt-cli/install-native-host.sh          # ff | chrome | all
```

This points the browser at `scripts/native-host-launcher.sh`, which runs the merged
host under the package venv (`ai_prompt_api/.venv`). The browser now starts/stops the
bridge for you.

### 2. Load the Extension

- Firefox: `about:debugging` → This Firefox → Load Temporary Add-on → `ai-prompt-extension/manifest.json`
- In the popup → Settings, keep **Native Messaging** on and set the **HTTP API Port** (default 8200).
- Log into at least one AI provider (ChatGPT, Grok, …).

### 3. Send a Prompt

```bash
curl -X POST http://localhost:8200/prompt \
  -H "Content-Type: application/json" \
  -d '{"text": "What is 2+2?", "ephemeral": true}'

# Or, direct to the extension with no HTTP at all:
cd ai-prompt-cli && ./ai-prompt.py "What is 2+2?"
```

### Alternative: standalone bridge (headless / legacy WebSocket)

```bash
./scripts/start-bridge.sh            # (re)start on :8200 (HTTP) + :8760 (extension WS)
./scripts/start-bridge.sh --status
./scripts/start-bridge.sh --stop
# PORT=8201 WS_PORT=8761 ./scripts/start-bridge.sh   # a second instance
```
In this mode turn **Native Messaging off** in the popup and set the **WebSocket Port** to match `WS_PORT`.

## Components

| Directory | Description | README |
|-----------|-------------|--------|
| `ai_prompt_api/` | REST API server | [ai_prompt_api/README.md](ai_prompt_api/README.md) |
| `ai-prompt-extension/` | Firefox browser extension | [ai-prompt-extension/README.md](ai-prompt-extension/README.md) |
| `ai-prompt-cli/` | Command-line client | [ai-prompt-cli/README.md](ai-prompt-cli/README.md) |

## Supported Providers

| Provider | Status | File Attachments |
|----------|--------|-----------------|
| ChatGPT | Working | Yes |
| Grok | Working | Yes (images) |
| DeepSeek | Working | Yes |
| Claude | Placeholder | No |

Providers are tried in configurable priority order with automatic failover.

## Configuration

The API reads environment variables with the `AI_PROMPT_` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_PROMPT_PORT` | 8000 | API server port |
| `AI_PROMPT_WS_PORT` | 8760 | WebSocket port for extension |
| `AI_PROMPT_HOST` | 0.0.0.0 | API listen address |
| `AI_PROMPT_API_KEY` | *(none)* | Optional Bearer token for auth |
| `AI_PROMPT_DEFAULT_TIMEOUT` | 120 | Prompt timeout in seconds |

## Requirements

- Python 3.8+
- Firefox
- Active login session with at least one supported AI provider
