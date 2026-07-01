# AI Provider Automator — Browser Extension

Firefox (MV2) and Chrome (MV3) extension that automates AI chat providers. Receives prompts via WebSocket or native messaging, opens provider tabs, injects text, waits for responses, and returns them.

## Supported Providers

| Provider | Status | Attachments | Models Detected |
|----------|--------|-------------|-----------------|
| ChatGPT | Working | Yes | gpt-4o, gpt-4, gpt-3.5-turbo, o1 |
| Grok | Working | Yes (images) | grok-2, grok-3 |
| DeepSeek | Working | Yes | deepseek-r1, deepseek-v3, deepseek-v2.5 |
| Claude | Placeholder | No | — |

Providers are tried in a configurable priority order. If one fails, the next is tried automatically.

## Installation

`manifest.json` is the currently active manifest — a copy of either `manifest.ff.json` (MV2) or
`manifest.chrome.json` (MV3). Switch with `./set-manifest.sh {ff|chrome}` before loading.

### Firefox

1. `./set-manifest.sh ff`
2. Open Firefox → `about:debugging` → This Firefox → Load Temporary Add-on
3. Select `manifest.json` from this directory
4. Log into at least one AI provider in Firefox

### Chrome

1. `./set-manifest.sh chrome`
2. Open Chrome → `chrome://extensions` → enable Developer mode → Load unpacked
3. Select this directory
4. Log into at least one AI provider in Chrome

`manifest.chrome.json` pins a `"key"` (a public key) so the unpacked extension's ID stays the
fixed value `jboncdafmgnneicfhnhfhmejnhdjlnhi` across reloads — this is required for the native
messaging host manifest's `allowed_origins` to keep matching. The matching private key lives one
level up at `../chrome-signing-key.pem` (gitignored, local-only; only needed if you ever want to
re-derive or rotate the key — Chrome itself never reads it, only the public `"key"` in the
manifest). It's deliberately kept outside this directory — Chrome's "Load unpacked" scans the
whole folder and warns about any bundled `.pem`/key file found inside it.

## Connection Methods

### Native Messaging (default, preferred)

The extension launches `native-host.py` (from `ai-prompt-cli/`) automatically when loaded. The CLI connects via Unix socket at `/tmp/ai-prompt-native.sock`.

**Setup:** Run `ai-prompt-cli/install-native-host.sh` (installs both browsers' host manifests by
default; pass `ff` or `chrome` to install just one), then restart the browser.

### WebSocket (fallback)

Connects to `ws://localhost:8760` (configurable). The API server or CLI starts the WebSocket server; the extension connects as a client.

If native messaging fails, the extension falls back to WebSocket automatically.

## Chrome (MV3) Notes

- **Service worker lifecycle:** Chrome can terminate an idle background service worker, which
  would drop the WebSocket/native connection. An `alarms`-based watchdog (every minute) checks the
  connection and reconnects if needed — this is what resurrects a terminated worker, since alarms
  are the one thing guaranteed to wake it back up. Firefox's background page is `persistent: true`
  and never unloads, so the watchdog is a no-op there.
- **`hideTabs` is Firefox-only:** it relies on `tabs.hide()`, which Chrome doesn't have. The
  setting is still there in Chrome but silently does nothing (tabs open normally, active).
- **Dynamic content-script injection** uses `chrome.scripting.executeScript` on Chrome (MV3 has no
  `tabs.executeScript`) vs. the old per-file `tabs.executeScript` loop on Firefox — see
  `injectContentScripts()` in `background.js`.

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
ai-prompt/
├── chrome-signing-key.pem   # Private key matching manifest.chrome.json's "key" (gitignored,
│                            # kept out of ai-prompt-extension/ so Chrome doesn't flag it)
└── ai-prompt-extension/
    ├── manifest.json          # Active manifest (copy of manifest.ff.json or manifest.chrome.json)
    ├── manifest.ff.json       # Firefox source manifest (MV2)
    ├── manifest.chrome.json   # Chrome source manifest (MV3)
    ├── set-manifest.sh        # Switches manifest.json between ff/chrome
    ├── background.js          # WebSocket/native messaging, provider orchestration
    ├── background-sw.js       # Chrome-only MV3 service worker entry (importScripts wrapper)
    ├── content.js             # DOM interaction, prompt execution, response extraction
    ├── logging.js             # Centralized logging, console hijacking
    ├── popup.html             # Popup UI
    ├── popup.js               # Popup logic (settings, logs, provider ordering)
    └── providers/
        ├── index.js        # Provider registry
        ├── chatgpt.js      # ChatGPT selectors and extraction
        ├── grok.js         # Grok selectors, image extraction
        ├── claude.js       # Claude placeholder
        └── deepseek.js     # DeepSeek selectors and input handling
```
