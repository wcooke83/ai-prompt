# AI Prompt API

FastAPI REST server that accepts prompt requests and forwards them to the browser extension via WebSocket.

## Setup

```bash
pip install -r requirements.txt
```

## Running

```bash
# Direct
uvicorn ai_prompt_api.main:app --host 0.0.0.0 --port 8000 --reload

# Or via module
python -m ai_prompt_api.main
```

Swagger docs available at `http://localhost:8000/docs`.

## Endpoints

### Health & Status (no auth)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | API health check, reports extension connection status |
| GET | `/status` | Detailed status: active provider, sessions, pending requests |

### Prompts (auth required if `AI_PROMPT_API_KEY` is set)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/prompt` | Send a prompt to an AI provider via the extension |

**Request body:**

```json
{
  "text": "Your prompt here",
  "provider": "chatgpt",
  "session_id": "sess_...",
  "timeout": 120,
  "ephemeral": true,
  "attachments": [
    { "name": "file.png", "type": "image/png", "data": "<base64>" }
  ]
}
```

Only `text` is required. When `ephemeral` is true, the provider tab closes after responding.

**Response:**

```json
{
  "success": true,
  "text": "Response text",
  "provider": "chatgpt",
  "model": "gpt-4o",
  "session_id": "sess_...",
  "input_tokens": 150,
  "output_tokens": 280,
  "images": []
}
```

### Sessions (auth required if `AI_PROMPT_API_KEY` is set)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/sessions` | List active sessions |
| GET | `/sessions/{id}` | Get session info |
| DELETE | `/sessions/{id}` | Delete a session |

## Configuration

Environment variables (prefix `AI_PROMPT_`):

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_PROMPT_PORT` | 8000 | REST API port |
| `AI_PROMPT_HOST` | 0.0.0.0 | Listen address |
| `AI_PROMPT_WS_PORT` | 8760 | WebSocket port for extension communication |
| `AI_PROMPT_API_KEY` | *(none)* | Bearer token for auth (disabled if unset) |
| `AI_PROMPT_DEFAULT_TIMEOUT` | 120.0 | Default prompt timeout in seconds |

Also reads from a `.env` file in the project root.

## Authentication

When `AI_PROMPT_API_KEY` is set, all prompt and session endpoints require a Bearer token:

```bash
curl -H "Authorization: Bearer your-key" http://localhost:8000/prompt ...
```

Health and status endpoints are always public.

## WebSocket Protocol

The API runs a WebSocket server on `ws://localhost:8760`. The browser extension connects to it.

**API → Extension (prompt):**
```json
{
  "type": "prompt",
  "request_id": "uuid",
  "text": "prompt text",
  "provider": "chatgpt",
  "session_id": "sess_...",
  "ephemeral": false,
  "attachments": []
}
```

**Extension → API (response):**
```json
{
  "type": "response",
  "request_id": "uuid",
  "success": true,
  "text": "response text",
  "provider": "chatgpt",
  "model": "gpt-4o",
  "session_id": "sess_...",
  "input_tokens": 150,
  "output_tokens": 280,
  "images": []
}
```

## Project Structure

```
ai_prompt_api/
├── main.py          # App entry point, lifespan management
├── config.py        # Settings via pydantic-settings
├── models.py        # Pydantic request/response models
├── auth.py          # Bearer token authentication
├── routers/
│   ├── health.py    # /health, /status
│   ├── prompts.py   # /prompt
│   └── sessions.py  # /sessions
└── services/
    └── extension.py # WebSocket server, ExtensionManager
```
