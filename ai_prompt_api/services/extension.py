"""WebSocket server for browser extension connection."""

import asyncio
import json
import uuid
from datetime import datetime
from typing import Dict, List, Optional

import websockets
from websockets.server import serve


class ExtensionManager:
    def __init__(self, port: int = 8760):
        self.port = port
        self.client = None
        self.server = None
        self.pending_requests: Dict[str, asyncio.Future] = {}
        self.sessions: Dict[str, dict] = {}

    async def start(self):
        """Start the WebSocket server."""
        self.server = await serve(self._handle_connection, "localhost", self.port)

    async def stop(self):
        """Stop the WebSocket server."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    @property
    def is_connected(self) -> bool:
        """Check if browser extension is connected."""
        return self.client is not None

    async def _handle_connection(self, websocket):
        """Handle incoming WebSocket connection from browser extension."""
        self.client = websocket
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    if data.get("type") == "response":
                        request_id = data.get("request_id")
                        if request_id in self.pending_requests:
                            self.pending_requests[request_id].set_result(data)
                except json.JSONDecodeError:
                    pass
        except websockets.ConnectionClosed:
            pass
        finally:
            self.client = None

    async def send_prompt(
        self,
        text: str,
        session_id: Optional[str] = None,
        provider: Optional[str] = None,
        ephemeral: bool = False,
        timeout: float = 120.0,
        attachments: Optional[List[dict]] = None,
    ) -> dict:
        """Send prompt to extension and wait for response."""
        if not self.client:
            return {"success": False, "error": "Extension not connected"}

        request_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self.pending_requests[request_id] = future

        message = {"type": "prompt", "request_id": request_id, "text": text}
        if session_id:
            message["session_id"] = session_id
        if provider:
            message["provider"] = provider
        if ephemeral:
            message["ephemeral"] = True
        if attachments:
            message["attachments"] = attachments

        try:
            await self.client.send(json.dumps(message))
            result = await asyncio.wait_for(future, timeout=timeout)

            # Track session
            if result.get("success") and result.get("session_id"):
                self.sessions[result["session_id"]] = {
                    "provider": result.get("provider"),
                    "created_at": datetime.now(),
                }

            return result
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Timeout after {timeout}s"}
        finally:
            self.pending_requests.pop(request_id, None)

    def get_session(self, session_id: str) -> Optional[dict]:
        """Get session info by ID."""
        return self.sessions.get(session_id)

    def list_sessions(self) -> Dict[str, dict]:
        """List all tracked sessions."""
        return self.sessions

    def delete_session(self, session_id: str) -> bool:
        """Delete a session from tracking."""
        if session_id in self.sessions:
            del self.sessions[session_id]
            return True
        return False
