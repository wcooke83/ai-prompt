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
