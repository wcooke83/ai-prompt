#  AI-Prompt CLI

Send prompts to AI providers (ChatGPT, Claude, Grok, DeepSeek) via browser extension.

## Two Modes of Operation

This tool supports two communication architectures:

1. **WebSocket Mode** (`ai-prompt.py`) - CLI starts server, extension connects
2. **Native Messaging Mode** (`ai-prompt-native.py`) - Extension launches native host, CLI connects via Unix socket

Choose WebSocket for standalone CLI usage. Choose Native Messaging for better Firefox integration (auto-launch when extension loads).

## Requirements

- Python 3.8+
- Firefox or Chrome with the AI Provider Automator extension loaded
- Active session with your chosen AI provider (ChatGPT, Claude, Grok, or DeepSeek)

## Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## WebSocket Mode (ai-prompt.py)

### Setup

No additional setup required. Just start the CLI - it will launch a WebSocket server and wait for the extension to connect.

### Usage

```bash
./ai-prompt.py "Your prompt here"
echo "Your prompt" | ./ai-prompt.py
```

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--json` | `-j` | Output as JSON (includes session_id, keeps tab open) |
| `--session ID` | `-s` | Continue existing conversation |
| `--provider NAME` | `-p` | Force provider: `chatgpt`, `claude`, `grok` |
| `--attach FILE` | `-a` | Attach file(s) to the prompt (can use multiple times) |
| `--port PORT` | | WebSocket port (default: 8760) |
| `--timeout SEC` | | Response timeout (default: 120) |
| `--verbose` | `-v` | Show connection status messages |
| `--log` | | Enable WebSocket message logging |
| `--no-log` | | Disable WebSocket message logging |

### Session Behavior

| Mode | Tab Behavior | Returns session_id |
|------|--------------|-------------------|
| Plain text (no flags) | Opens new tab, closes after response | No |
| `--json` | Opens new tab, keeps open | Yes |
| `--session ID` | Uses existing tab | Yes |

### Examples

```bash
# Simple one-off query (tab closes after)
./ai-prompt.py "What is 2+2?"

# Start new conversation, get session ID for later
./ai-prompt.py --json "Hello, my name is Bob"
# {"success": true, "text": "Hi Bob!", "session_id": "sess_abc123", "provider": "chatgpt"}

# Continue conversation using session ID
./ai-prompt.py --json --session sess_abc123 "What is my name?"
# {"success": true, "text": "Bob", "session_id": "sess_abc123", "provider": "chatgpt"}

# Pipe input
cat prompt.txt | ./ai-prompt.py

# Attach a file (currently supported: Grok)
./ai-prompt.py -a image.png "Describe this image"

# Attach multiple files
./ai-prompt.py -a file1.txt -a file2.txt "Compare these files"
```

### Configuration

Create a `.env` file (copy from `.env.example`):

```bash
AI_PROMPT_PORT=8760
AI_PROMPT_TIMEOUT=120
AI_PROMPT_LOG_ENABLED=true
AI_PROMPT_LOG_DIR=./logs
AI_PROMPT_DEFAULT_PROVIDER=
```

Logs are written to `logs/ws_YYYYMMDD_HHMMSS.log` when enabled.

---

## Native Messaging Mode (ai-prompt-native.py)

### Setup

1. **Install the native messaging host(s):**
   ```bash
   ./install-native-host.sh        # installs for both Firefox and Chrome
   ./install-native-host.sh ff     # Firefox only
   ./install-native-host.sh chrome # Chrome only
   ```
   Firefox: `~/.mozilla/native-messaging-hosts/ai_prompt_native.json` (Linux) or `~/Library/Application Support/Mozilla/NativeMessagingHosts/ai_prompt_native.json` (macOS).

   Chrome: `~/.config/google-chrome/NativeMessagingHosts/ai_prompt_native.json` (Linux) or `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/ai_prompt_native.json` (macOS). The `allowed_origins` entry is derived automatically from the pinned extension ID in `../ai-prompt-extension/manifest.chrome.json`.

2. **Restart the browser and reload the extension**
   - Firefox: `about:debugging` → This Firefox → Reload the AI Provider Automator extension
   - Chrome: `chrome://extensions` → Reload the AI Provider Automator extension
   - The extension will automatically launch `native-host.py` when it loads
   - Check the browser console for "Native host connected" message

3. **Verify connection:**
   ```bash
   ./ai-prompt-native.py --status
   ```
   Should show: `Native host: connected to extension`

### Usage

Same interface as WebSocket mode, but no server startup needed:

```bash
./ai-prompt-native.py "Your prompt here"
echo "Your prompt" | ./ai-prompt-native.py
```

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--json` | `-j` | Output as JSON (includes session_id, keeps tab open) |
| `--session ID` | `-s` | Continue existing conversation |
| `--provider NAME` | `-p` | Force provider: `chatgpt`, `claude`, `grok`, `deepseek` |
| `--timeout SEC` | | Response timeout (default: 120) |
| `--status` | | Check native host connection status |

### Examples

```bash
# Check if native host is connected
./ai-prompt-native.py --status

# Simple query
./ai-prompt-native.py "What is 2+2?"

# Start conversation with session tracking
./ai-prompt-native.py --json "Hello, I'm working on a Python project"

# Continue conversation
./ai-prompt-native.py --json --session sess_abc123 "Can you help me debug it?"

# Force specific provider
./ai-prompt-native.py --provider claude "Explain closures in JavaScript"
```

### How It Works

1. Firefox extension loads → launches `native-host.py` (via native messaging protocol)
2. `native-host.py` creates Unix socket at `/tmp/ai-prompt-native.sock`
3. CLI (`ai-prompt-native.py`) connects to socket → sends prompt
4. `native-host.py` forwards to extension via stdin/stdout
5. Extension processes prompt → returns response
6. `native-host.py` forwards response back to CLI

### Troubleshooting

**"Native host not running" error:**
- Ensure Firefox is open with the extension loaded
- Check extension console for errors: `about:debugging` → Inspect
- Verify manifest installed: `cat ~/.mozilla/native-messaging-hosts/ai_prompt_native.json`
- Check native-host.py is executable: `chmod +x native-host.py`

**Extension not connecting:**
- Check `extension_id` in manifest matches your extension ID
- Look for "Native host connected" in browser console
- Restart Firefox after installing native host

**Permission denied on socket:**
- Socket is at `/tmp/ai-prompt-native.sock` with permissions 0600
- Only the user who launched Firefox can connect

---

## Comparison: WebSocket vs Native Messaging

| Feature | WebSocket Mode | Native Messaging Mode |
|---------|----------------|----------------------|
| **Setup** | None | Run `install-native-host.sh` once |
| **Launch** | CLI starts first | Extension auto-launches native host |
| **Connection** | Extension connects to CLI | CLI connects to native host socket |
| **Port config** | Configurable (`--port`) | Fixed Unix socket path |
| **Best for** | Standalone CLI workflows | Integrated with Firefox workflows |
| **Requires Firefox open** | No (CLI waits for connection) | Yes (native host only runs when Firefox is open) |
