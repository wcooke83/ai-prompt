#!/bin/bash
# Install native messaging host(s) for AI Provider Automator
# Usage: ./install-native-host.sh [ff|chrome|all]   (default: all)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_SCRIPT="$SCRIPT_DIR/native-host.py"
MANIFEST_NAME="ai_prompt_native.json"
TARGET="${1:-all}"

# Make host script executable
chmod +x "$HOST_SCRIPT"

install_ff() {
  if [[ "$OSTYPE" == "darwin"* ]]; then
    MANIFEST_DIR="$HOME/Library/Application Support/Mozilla/NativeMessagingHosts"
  elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    MANIFEST_DIR="$HOME/.mozilla/native-messaging-hosts"
  else
    echo "Unsupported OS for Firefox: $OSTYPE"
    return 1
  fi

  mkdir -p "$MANIFEST_DIR"
  cat > "$MANIFEST_DIR/$MANIFEST_NAME" << EOF
{
  "name": "ai_prompt_native",
  "description": "Native messaging host for AI Provider Automator",
  "path": "$HOST_SCRIPT",
  "type": "stdio",
  "allowed_extensions": ["ai-provider-automator@localhost"]
}
EOF
  echo "Firefox native host installed: $MANIFEST_DIR/$MANIFEST_NAME"
}

install_chrome() {
  if [[ "$OSTYPE" == "darwin"* ]]; then
    MANIFEST_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
  elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    MANIFEST_DIR="$HOME/.config/google-chrome/NativeMessagingHosts"
  else
    echo "Unsupported OS for Chrome: $OSTYPE"
    return 1
  fi

  # Chrome (unlike Firefox) has no add-on ID field, so the unpacked extension's ID is pinned via
  # the "key" in manifest.chrome.json instead. Derive the ID from that key so this host manifest
  # can never drift out of sync with the extension's actual ID.
  EXT_MANIFEST="$SCRIPT_DIR/../ai-prompt-extension/manifest.chrome.json"
  if [[ ! -f "$EXT_MANIFEST" ]]; then
    echo "Cannot find $EXT_MANIFEST to derive the Chrome extension ID; skipping Chrome install."
    return 1
  fi

  EXT_ID=$(python3 - "$EXT_MANIFEST" << 'PYEOF'
import base64, hashlib, json, sys
manifest = json.load(open(sys.argv[1]))
der = base64.b64decode(manifest["key"])
h = hashlib.sha256(der).hexdigest()[:32]
print("".join(chr(ord("a") + int(c, 16)) for c in h))
PYEOF
)
  if [[ -z "$EXT_ID" ]]; then
    echo "Failed to derive Chrome extension ID; skipping Chrome install."
    return 1
  fi

  mkdir -p "$MANIFEST_DIR"
  cat > "$MANIFEST_DIR/$MANIFEST_NAME" << EOF
{
  "name": "ai_prompt_native",
  "description": "Native messaging host for AI Provider Automator",
  "path": "$HOST_SCRIPT",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
EOF
  echo "Chrome native host installed: $MANIFEST_DIR/$MANIFEST_NAME (extension id: $EXT_ID)"
}

case "$TARGET" in
  ff|firefox)
    install_ff
    ;;
  chrome)
    install_chrome
    ;;
  all)
    install_ff
    install_chrome
    ;;
  *)
    echo "Usage: $0 [ff|chrome|all]"
    exit 1
    ;;
esac

echo ""
echo "Now restart the browser(s) and reload the extension."
