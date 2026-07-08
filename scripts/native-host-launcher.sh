#!/bin/bash
#
# Native-messaging entry point. The BROWSER execs this (path is set in the
# native-messaging manifest by scripts/install-native-host.sh). It runs the
# merged native host — native messaging to the extension PLUS the HTTP bridge —
# under the package's venv (ai_prompt_api/.venv, which has fastapi/uvicorn).
#
# Native messaging requires a pristine stdout: this script must NOT print
# anything, hence `exec` straight into Python with no echoes. All host
# diagnostics go to stderr (browser console / stderr log).

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$REPO/ai_prompt_api/.venv/bin/python"

exec env PYTHONPATH="$REPO" "$VENV_PY" -m ai_prompt_api.native_host
