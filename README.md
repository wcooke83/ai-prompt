# AI Prompt

Send prompts to AI chat providers (ChatGPT, Grok, Claude, DeepSeek) programmatically. A REST API accepts prompt requests and forwards them to a Firefox browser extension that automates the provider's web interface, extracts the response, and returns it.

## Architecture

```
CLI / HTTP Client
      │
      ▼
  REST API (FastAPI)  ◄──►  WebSocket  ◄──►  Firefox Extension  ◄──►  AI Provider Tab
  :8000                      :8760              (content scripts)       (ChatGPT, Grok, etc.)
```

1. **API** (`ai_prompt_api/`) — FastAPI server exposing REST endpoints for prompts, sessions, and health checks. Communicates with the extension over a WebSocket server.
2. **Extension** (`ai-prompt-extension/`) — Firefox extension that receives prompts via WebSocket or native messaging, opens AI provider tabs, injects text, waits for responses, and sends them back.
3. **CLI** (`ai-prompt-cli/`) — Command-line client that can connect directly to the extension (WebSocket or native messaging) for quick scripting without the API.

## Quick Start

### 1. Start the API

```bash
cd ai_prompt_api
pip install -r requirements.txt
uvicorn ai_prompt_api.main:app --host 0.0.0.0 --port 8000
```

### 2. Load the Extension

- Open Firefox → `about:debugging` → This Firefox → Load Temporary Add-on
- Select `ai-prompt-extension/manifest.json`
- Ensure you're logged into at least one AI provider (ChatGPT, Grok, etc.)

### 3. Send a Prompt

```bash
# Via API
curl -X POST http://localhost:8000/prompt \
  -H "Content-Type: application/json" \
  -d '{"text": "What is 2+2?", "ephemeral": true}'

# Via CLI (direct to extension, no API needed)
cd ai-prompt-cli
./ai-prompt.py "What is 2+2?"
```

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
