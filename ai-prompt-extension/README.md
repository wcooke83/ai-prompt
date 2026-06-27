# AI Provider Automator — Firefox Extension

Firefox extension (Manifest V2) that automates AI chat providers. Receives prompts via WebSocket or native messaging, opens provider tabs, injects text, waits for responses, and returns them.

## Supported Providers

| Provider | Status | Attachments | Models Detected |
|----------|--------|-------------|-----------------|
| ChatGPT | Working | Yes | gpt-4o, gpt-4, gpt-3.5-turbo, o1 |
| Grok | Working | Yes (images) | grok-2, grok-3 |
| DeepSeek | Working | Yes | deepseek-r1, deepseek-v3, deepseek-v2.5 |
| Claude | Placeholder | No | — |

Providers are tried in a configurable priority order. If one fails, the next is tried automatically.

## Installation

1. Open Firefox → `about:debugging` → This Firefox → Load Temporary Add-on
2. Select `manifest.json` from this directory
3. Log into at least one AI provider in Firefox

## Connection Methods

### Native Messaging (default, preferred)

The extension launches `native-host.py` (from `ai-prompt-cli/`) automatically when loaded. The CLI connects via Unix socket at `/tmp/ai-prompt-native.sock`.

**Setup:** Run `ai-prompt-cli/install-native-host.sh` once, then restart Firefox.

### WebSocket (fallback)

Connects to `ws://localhost:8760` (configurable). The API server or CLI starts the WebSocket server; the extension connects as a client.

If native messaging fails, the extension falls back to WebSocket automatically.

## How It Works

1. Receives a prompt message (via WebSocket or native messaging)
2. Selects a provider based on priority order and availability
3. Opens a new tab (or reuses an existing session tab)
4. Injects the content script which:
   - Finds the textarea using provider-specific selectors
   - Uploads any file attachments
   - Types the prompt text (keystroke simulation or paste)
   - Clicks the send button
   - Waits for the response to complete (DOM stability + streaming detection)
   - Extracts the response text (and images for Grok)
5. Returns the response with metadata (provider, model, token estimates, session ID)
6. Closes the tab if ephemeral, or keeps it for session continuity

## Popup UI

The popup has three tabs:

- **Provider Priority** — Drag to reorder providers. Shows connection status and active provider.
- **Settings** — Connection method (native/WebSocket), port, fast reconnect, keep tabs open, paste input mode, DOM stability timeout, debug logging.
- **Logs** — Live log viewer with color-coded entries. Useful for debugging provider interactions.

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| Connection method | Native messaging | Native messaging or WebSocket |
| WebSocket port | 8760 | Port for WebSocket connection |
| Fast reconnect | On | 500ms constant polling vs exponential backoff |
| Keep tabs open | Off | Override ephemeral — never close tabs |
| Use paste input | Off | Paste text instead of keystroke simulation |
| DOM stability (ms) | 3000 | Wait time for DOM inactivity before extracting response |
| Debug logging | Off | Verbose logging in content scripts |

## Message Protocol

**Incoming (prompt):**
```json
{
  "type": "prompt",
  "request_id": "unique-id",
  "text": "prompt text",
  "provider": "chatgpt",
  "session_id": "sess_...",
  "ephemeral": true,
  "attachments": [
    { "name": "file.png", "type": "image/png", "data": "<base64>" }
  ]
}
```

**Outgoing (response):**
```json
{
  "type": "response",
  "request_id": "unique-id",
  "success": true,
  "text": "response text",
  "images": [{ "src": "url", "alt": "text" }],
  "provider": "chatgpt",
  "model": "gpt-4o",
  "input_tokens": 150,
  "output_tokens": 280,
  "session_id": "sess_..."
}
```

## Project Structure

```
ai-prompt-extension/
├── manifest.json       # Extension manifest (v2)
├── background.js       # WebSocket/native messaging, provider orchestration
├── content.js          # DOM interaction, prompt execution, response extraction
├── logging.js          # Centralized logging, console hijacking
├── popup.html          # Popup UI
├── popup.js            # Popup logic (settings, logs, provider ordering)
└── providers/
    ├── index.js        # Provider registry
    ├── chatgpt.js      # ChatGPT selectors and extraction
    ├── grok.js         # Grok selectors, image extraction
    ├── claude.js       # Claude placeholder
    └── deepseek.js     # DeepSeek selectors and input handling
```
