# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AI-Prompt is a system for sending prompts to AI chat providers (ChatGPT, Claude, Grok) via a Firefox browser extension controlled by a Python CLI over WebSocket.

## Architecture

```
ai-prompt-cli/        Python WebSocket server CLI
  ai-prompt.py        Main CLI - starts WS server, waits for extension, sends prompts

ai-prompt-extension/  Firefox extension (Manifest V2)
  background.js       WebSocket client, session management, tab orchestration
  content.js          DOM interaction - finds elements, types text, extracts responses
  providers/          Provider-specific selectors and detection logic
    index.js          ProviderRegistry - URL matching, provider lookup
    chatgpt.js        ChatGPT selectors and response extraction (fully implemented)
    claude.js         Claude selectors (stub)
    grok.js           Grok selectors (stub)
  popup.html/js       Extension UI for connection status and settings
```

**Flow**: CLI starts WebSocket server → Extension connects → CLI sends prompt → Background script opens/reuses tab → Content script types prompt, clicks send, waits for response → Response returned via WebSocket → CLI prints result.

**Sessions**: `--json` flag keeps tabs open and returns `session_id` for conversation continuity. Ephemeral mode (default without `--json`) closes tab after response.

## HTTP Bridge (for apps, e.g. securities)

Apps POST to a REST API (`ai_prompt_api/routers/`: `/prompt`, `/status`, `/sessions`, `/health`). The routers read the active transport from `ai_prompt_api/state.py` (`extension_manager`), so either transport can back them:

- **`ai_prompt_api/native_host.py` (recommended, browser-managed).** The browser auto-launches this via the native-messaging manifest (`scripts/install-native-host.sh` → `scripts/native-host-launcher.sh`, which runs it under the package venv `ai_prompt_api/.venv`). One process = native messaging to the extension (stdio) + the unix socket for the CLI + a uvicorn HTTP server. **The extension dictates the HTTP port**: after `host_ready` it sends `{type:"ready"}` then `{type:"configure",httpPort}`, and the host binds/rebinds there. So per-browser ports are set in the popup ("HTTP API Port"), nothing on the shell side. **stdout is the native-messaging wire** — `native_host.py` repoints `sys.stdout`→stderr at import so uvicorn logs can't corrupt framing. Writes a discovery file at `~/.ai-prompt/instances/<pid>.json`.
- **`ai_prompt_api/main.py` (standalone, legacy/headless).** `scripts/start-bridge.sh` runs uvicorn on `AI_PROMPT_PORT` (8200) with a WebSocket `ExtensionManager` on `AI_PROMPT_WS_PORT` (8760) the extension dials as a client. Use when the browser isn't managing the host.

**No hot-reload** on either — restart (or reload the extension for the native host) after editing bridge Python. Config env vars use the `AI_PROMPT_` prefix (see README).

## Commands

### CLI Setup & Usage
```bash
cd ai-prompt-cli
python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt
./ai-prompt.py "prompt"              # One-off query (tab closes)
./ai-prompt.py --json "prompt"       # Returns session_id, keeps tab
./ai-prompt.py -s <session_id> "..."  # Continue conversation
./ai-prompt.py -p chatgpt "..."      # Force specific provider
```

### Extension Installation
Load `ai-prompt-extension/` as temporary add-on in Firefox (`about:debugging` → Load Temporary Add-on → select `manifest.json`).

## Key Implementation Details

- **WebSocket port**: 8760 (default), configurable via `--port` and extension popup
- **Response detection**: Uses MutationObserver + polling, waits for streaming indicators to disappear and action buttons (Copy button) to appear
- **Text input**: Simulates paste via ClipboardEvent/execCommand rather than keystroke simulation
- **Provider detection**: Matches current tab URL against `hostPatterns` arrays in provider configs

## Adding a New Provider

1. Create `providers/<name>.js` with `hostPatterns`, `selectors` (textarea, sendButton, responseContainer, streamingIndicator, markdownContent), and optionally `detectStreamingComplete`/`extractResponseText` functions
2. Register in `background.js`: `ProviderRegistry.register(<Name>Provider)`
3. Add URL patterns to `manifest.json` permissions and content_scripts.matches
4. Add to CLI `--provider` choices in `ai-prompt.py`
