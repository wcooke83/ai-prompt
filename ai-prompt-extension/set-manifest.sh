#!/bin/bash
# Toggle manifest.json between Firefox (MV2) and Chrome (MV3)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

case "$1" in
  ff|firefox)
    cp manifest.ff.json manifest.json
    echo "Switched to Firefox (MV2)"
    ;;
  chrome)
    cp manifest.chrome.json manifest.json
    echo "Switched to Chrome (MV3)"
    ;;
  *)
    echo "Usage: $0 {ff|chrome}"
    echo "  ff     - Firefox Manifest V2"
    echo "  chrome - Chrome Manifest V3"
    exit 1
    ;;
esac
