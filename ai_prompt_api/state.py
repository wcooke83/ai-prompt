"""Shared reference to the active extension manager.

The routers talk to whichever transport is running through this single module-level
handle, so they don't need to know which one is live:

  - the standalone WebSocket bridge (main.py) plugs in an ExtensionManager
  - the browser-managed native host (native_host.py) plugs in a NativeExtensionBridge

Both expose the same duck-typed interface:
    is_connected: bool
    pending_requests: dict
    sessions: dict
    async send_prompt(text, session_id=None, provider=None, ephemeral=False,
                      timeout=120.0, attachments=None) -> dict
    list_sessions() -> dict
    get_session(session_id) -> dict | None
    delete_session(session_id) -> bool
"""

extension_manager = None
