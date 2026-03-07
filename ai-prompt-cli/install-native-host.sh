#!/bin/bash
# Install native messaging host for Firefox

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_SCRIPT="$SCRIPT_DIR/native-host.py"
MANIFEST_NAME="ai_prompt_native.json"

# Make host script executable
chmod +x "$HOST_SCRIPT"

# Determine manifest directory based on OS
if [[ "$OSTYPE" == "darwin"* ]]; then
    MANIFEST_DIR="$HOME/Library/Application Support/Mozilla/NativeMessagingHosts"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    MANIFEST_DIR="$HOME/.mozilla/native-messaging-hosts"
else
    echo "Unsupported OS: $OSTYPE"
    exit 1
fi

# Create directory if it doesn't exist
mkdir -p "$MANIFEST_DIR"

# Create manifest with correct path
cat > "$MANIFEST_DIR/$MANIFEST_NAME" << EOF
{
  "name": "ai_prompt_native",
  "description": "Native messaging host for AI Provider Automator",
  "path": "$HOST_SCRIPT",
  "type": "stdio",
  "allowed_extensions": ["ai-provider-automator@localhost"]
}
EOF

echo "Native messaging host installed!"
echo "Manifest: $MANIFEST_DIR/$MANIFEST_NAME"
echo "Host: $HOST_SCRIPT"
echo ""
echo "Now restart Firefox and reload the extension."
