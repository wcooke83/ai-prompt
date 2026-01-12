#!/usr/bin/env python3
"""CLI tool to send prompts to AI providers via browser extension WebSocket."""

import argparse
import asyncio
import base64
import json
import logging
import mimetypes
import os
import sys
import uuid
import time
import errno
import platform
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import websockets

from config import get_config

# Platform-specific imports for file locking
if platform.system() == 'Windows':
    import msvcrt
else:
    import fcntl

# Suppress noisy websocket handshake errors from fast reconnect attempts
logging.getLogger("websockets").setLevel(logging.CRITICAL)

# Module-level logger for WebSocket message logging
ws_logger: Optional[logging.Logger] = None


def setup_ws_logger(log_dir: str) -> logging.Logger:
    """Set up a file logger for WebSocket messages."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("ai_prompt_ws")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_path / f"ws_{timestamp}.log"

    handler = logging.FileHandler(log_file)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s | %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


def read_file_as_attachment(file_path: str) -> dict:
    """Read a file and return attachment data with base64 content."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type is None:
        mime_type = "application/octet-stream"

    with open(path, "rb") as f:
        content = base64.b64encode(f.read()).decode("utf-8")

    return {
        "name": path.name,
        "type": mime_type,
        "data": content
    }


class QueueManager:
    """Manages file-based request queue for handling concurrent script invocations."""
    
    def __init__(self, queue_dir: str, max_queue_size: int):
        self.queue_dir = Path(queue_dir)
        self.requests_dir = self.queue_dir / "requests"
        self.responses_dir = self.queue_dir / "responses"
        self.lock_file = self.queue_dir / "port.lock"
        self.max_queue_size = max_queue_size
        self.lock_fd = None
        
        # Create directories
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.responses_dir.mkdir(parents=True, exist_ok=True)
        
        # Clean up stale files older than 1 hour
        self._cleanup_stale_files()
    
    def _cleanup_stale_files(self):
        """Remove stale request and response files older than 1 hour."""
        cutoff_time = time.time() - 3600  # 1 hour ago
        
        for file_path in self.requests_dir.glob("*.json"):
            try:
                if file_path.stat().st_mtime < cutoff_time:
                    file_path.unlink()
            except OSError:
                pass
        
        for file_path in self.responses_dir.glob("*.json"):
            try:
                if file_path.stat().st_mtime < cutoff_time:
                    file_path.unlink()
            except OSError:
                pass
    
    def try_acquire_lock(self) -> bool:
        """Try to acquire the port lock (non-blocking)."""
        try:
            self.lock_fd = open(self.lock_file, 'w')
            
            if platform.system() == 'Windows':
                # Windows: use msvcrt for file locking
                try:
                    msvcrt.locking(self.lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as e:
                    if e.errno in (errno.EACCES, errno.EAGAIN):
                        if self.lock_fd:
                            self.lock_fd.close()
                            self.lock_fd = None
                        return False
                    raise
            else:
                # Unix/Linux: use fcntl for file locking
                try:
                    fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (IOError, OSError) as e:
                    if e.errno in (errno.EACCES, errno.EAGAIN):
                        if self.lock_fd:
                            self.lock_fd.close()
                            self.lock_fd = None
                        return False
                    raise
            
            self.lock_fd.write(str(os.getpid()))
            self.lock_fd.flush()
            return True
            
        except Exception as e:
            if self.lock_fd:
                try:
                    self.lock_fd.close()
                except:
                    pass
                self.lock_fd = None
            raise
    
    def release_lock(self):
        """Release the port lock."""
        if self.lock_fd:
            try:
                if platform.system() == 'Windows':
                    msvcrt.locking(self.lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_UN)
                self.lock_fd.close()
            except OSError:
                pass
            finally:
                self.lock_fd = None
            
            # Remove lock file
            try:
                self.lock_file.unlink()
            except OSError:
                pass
    
    def add_to_queue(self, request_data: dict) -> str:
        """Add request to queue and return request ID."""
        # Check queue size
        existing_requests = list(self.requests_dir.glob("*.json"))
        if len(existing_requests) >= self.max_queue_size:
            raise RuntimeError(f"Queue full (max {self.max_queue_size} requests)")
        
        request_id = str(uuid.uuid4())
        request_file = self.requests_dir / f"{request_id}.json"
        
        with open(request_file, 'w') as f:
            json.dump(request_data, f)
        
        return request_id
    
    def get_queued_requests(self) -> List[tuple]:
        """Get all queued requests as (request_id, data) tuples, oldest first."""
        requests = []
        for file_path in sorted(self.requests_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                with open(file_path, 'r') as f:
                    data = json.load(f)
                request_id = file_path.stem
                requests.append((request_id, data))
            except (OSError, json.JSONDecodeError):
                # Skip corrupt files
                continue
        return requests
    
    def remove_request(self, request_id: str):
        """Remove a request from the queue."""
        request_file = self.requests_dir / f"{request_id}.json"
        try:
            request_file.unlink()
        except OSError:
            pass
    
    def write_response(self, request_id: str, response_data: dict):
        """Write response for a queued request."""
        response_file = self.responses_dir / f"{request_id}.json"
        with open(response_file, 'w') as f:
            json.dump(response_data, f)
    
    def wait_for_response(self, request_id: str, timeout: float) -> dict:
        """Wait for response file to appear and return its contents."""
        response_file = self.responses_dir / f"{request_id}.json"
        start_time = time.time()
        poll_interval = 0.1
        
        while time.time() - start_time < timeout:
            if response_file.exists():
                try:
                    with open(response_file, 'r') as f:
                        response = json.load(f)
                    # Clean up response file
                    response_file.unlink()
                    return response
                except (OSError, json.JSONDecodeError):
                    # File might be mid-write, try again
                    time.sleep(poll_interval)
                    continue
            
            time.sleep(poll_interval)
        
        raise TimeoutError(f"No response received within {timeout}s")


class ChatGPTCLI:
    def __init__(self, port: int, connection_timeout: float, response_timeout: float, 
                 verbose: bool = False, ws_logger: Optional[logging.Logger] = None,
                 queue_manager: Optional[QueueManager] = None):
        self.port = port
        self.connection_timeout = connection_timeout
        self.response_timeout = response_timeout
        self.verbose = verbose
        self.ws_logger = ws_logger
        self.queue_manager = queue_manager
        self.client = None
        self.response_event = asyncio.Event()
        self.response_data: Optional[dict] = None
        self.pending_request_id: Optional[str] = None

    def log(self, message: str):
        """Print message only if verbose mode is enabled."""
        if self.verbose:
            print(message, file=sys.stderr)

    def log_ws(self, direction: str, data: dict):
        """Log WebSocket message to file if logging is enabled."""
        if self.ws_logger:
            self.ws_logger.info(f"{direction} | {json.dumps(data, indent=2)}")

    async def handle_connection(self, websocket):
        """Handle incoming WebSocket connection from browser extension."""
        self.client = websocket
        self.log("Extension connected")

        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    self.log_ws("RECV", data)
                    if data.get("type") == "response" and data.get("request_id") == self.pending_request_id:
                        self.response_data = data
                        self.response_event.set()
                except json.JSONDecodeError:
                    print("Invalid JSON received", file=sys.stderr)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.client = None
            self.log("Extension disconnected")

    async def send_prompt(self, prompt: str, session_id: Optional[str] = None, 
                         provider: Optional[str] = None, ephemeral: bool = False, 
                         attachments: Optional[List[dict]] = None) -> dict:
        """Send prompt and wait for response."""
        if not self.client:
            raise RuntimeError("No extension connected")

        request_id = str(uuid.uuid4())
        self.pending_request_id = request_id
        self.response_event.clear()
        self.response_data = None

        message = {
            "type": "prompt",
            "request_id": request_id,
            "text": prompt
        }
        if session_id:
            message["session_id"] = session_id
        elif ephemeral:
            message["ephemeral"] = True  # Close tab after response
        if provider:
            message["provider"] = provider
        if attachments:
            message["attachments"] = attachments

        self.log_ws("SEND", message)
        await self.client.send(json.dumps(message))

        try:
            await asyncio.wait_for(self.response_event.wait(), timeout=self.response_timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(f"Response timeout after {self.response_timeout}s")

        if self.response_data:
            return self.response_data

        raise RuntimeError("No response received")

    async def process_queue(self):
        """Process queued requests while server is running."""
        if not self.queue_manager:
            return
        
        while True:
            await asyncio.sleep(0.5)  # Check queue every 500ms
            
            # Get queued requests
            queued = self.queue_manager.get_queued_requests()
            if not queued:
                continue
            
            for request_id, request_data in queued:
                self.log(f"Processing queued request {request_id}")
                
                try:
                    # Wait for new connection if needed
                    if not self.client:
                        self.log("Waiting for extension connection for queued request...")
                        wait_start = time.time()
                        while not self.client and (time.time() - wait_start) < self.connection_timeout:
                            await asyncio.sleep(0.1)
                        
                        if not self.client:
                            raise RuntimeError("Extension not connected (timeout)")
                    
                    # Send the queued prompt
                    response = await self.send_prompt(
                        prompt=request_data["prompt"],
                        session_id=request_data.get("session_id"),
                        provider=request_data.get("provider"),
                        ephemeral=request_data.get("ephemeral", False),
                        attachments=request_data.get("attachments")
                    )
                    
                    # Write response
                    self.queue_manager.write_response(request_id, response)
                    self.log(f"Completed queued request {request_id}")
                    
                except Exception as e:
                    # Write error response
                    error_response = {
                        "success": False,
                        "error": str(e)
                    }
                    self.queue_manager.write_response(request_id, error_response)
                    self.log(f"Error processing queued request {request_id}: {e}")
                
                finally:
                    # Remove from queue
                    self.queue_manager.remove_request(request_id)

    async def run_as_server(self, prompt: str, session_id: Optional[str] = None, 
                           provider: Optional[str] = None, json_output: bool = False, 
                           attachments: Optional[List[dict]] = None):
        """Run as the main server process."""
        server = await websockets.serve(self.handle_connection, "localhost", self.port)
        self.log(f"Server started on ws://localhost:{self.port}")

        # Start queue processor
        queue_task = None
        if self.queue_manager:
            queue_task = asyncio.create_task(self.process_queue())

        try:
            # Wait for client connection
            self.log("Waiting for extension to connect...")
            wait_start = time.time()
            while not self.client and (time.time() - wait_start) < self.connection_timeout:
                await asyncio.sleep(0.1)

            if not self.client:
                if json_output:
                    print(json.dumps({"success": False, "error": "Extension not connected (timeout)"}))
                else:
                    print("Error: Extension not connected (timeout)", file=sys.stderr)
                return False

            # Process own request
            ephemeral = not json_output and not session_id
            response = await self.send_prompt(prompt, session_id, provider, ephemeral, attachments)

            if json_output:
                output = {
                    "success": response.get("success", False),
                    "text": response.get("text", ""),
                    "session_id": response.get("session_id"),
                    "provider": response.get("provider"),
                    "model": response.get("model"),
                    "input_tokens": response.get("input_tokens"),
                    "output_tokens": response.get("output_tokens")
                }
                images = response.get("images", [])
                if images:
                    output["images"] = images
                if response.get("error"):
                    output["error"] = response["error"]
                print(json.dumps(output))
            else:
                if response.get("success"):
                    print(response.get("text", ""))
                    images = response.get("images", [])
                    if images:
                        print()
                        for img in images:
                            src = img.get("src", "") if isinstance(img, dict) else img
                            if src:
                                print(src)
                else:
                    print(f"Error: {response.get('error', 'Unknown error')}", file=sys.stderr)
                    return False

            return True

        except RuntimeError as e:
            if json_output:
                print(json.dumps({"success": False, "error": str(e)}))
            else:
                print(f"Error: {e}", file=sys.stderr)
            return False
        finally:
            if queue_task:
                queue_task.cancel()
                try:
                    await queue_task
                except asyncio.CancelledError:
                    pass
            
            server.close()
            await server.wait_closed()

    async def run_as_client(self, prompt: str, session_id: Optional[str] = None,
                           provider: Optional[str] = None, json_output: bool = False,
                           attachments: Optional[List[dict]] = None):
        """Run as a queued client waiting for response."""
        if not self.queue_manager:
            raise RuntimeError("Queue manager not initialized")
        
        # Prepare request data
        request_data = {
            "prompt": prompt,
            "session_id": session_id,
            "provider": provider,
            "ephemeral": not json_output and not session_id,
            "attachments": attachments
        }
        
        try:
            # Add to queue
            request_id = self.queue_manager.add_to_queue(request_data)
            self.log(f"Added to queue with ID {request_id}")
            
            # Wait for response with timeout (connection + response time)
            total_timeout = self.connection_timeout + self.response_timeout
            response = self.queue_manager.wait_for_response(request_id, total_timeout)
            
            # Output response
            if json_output:
                output = {
                    "success": response.get("success", False),
                    "text": response.get("text", ""),
                    "session_id": response.get("session_id"),
                    "provider": response.get("provider"),
                    "model": response.get("model"),
                    "input_tokens": response.get("input_tokens"),
                    "output_tokens": response.get("output_tokens")
                }
                images = response.get("images", [])
                if images:
                    output["images"] = images
                if response.get("error"):
                    output["error"] = response["error"]
                print(json.dumps(output))
            else:
                if response.get("success"):
                    print(response.get("text", ""))
                    images = response.get("images", [])
                    if images:
                        print()
                        for img in images:
                            src = img.get("src", "") if isinstance(img, dict) else img
                            if src:
                                print(src)
                else:
                    print(f"Error: {response.get('error', 'Unknown error')}", file=sys.stderr)
                    return False
            
            return True
            
        except TimeoutError:
            error_msg = f"Timeout waiting for response ({self.connection_timeout + self.response_timeout}s)"
            if json_output:
                print(json.dumps({"success": False, "error": error_msg}))
            else:
                print(f"Error: {error_msg}", file=sys.stderr)
            
            # Clean up queued request
            try:
                self.queue_manager.remove_request(request_id)
            except:
                pass
            
            return False
        
        except RuntimeError as e:
            if json_output:
                print(json.dumps({"success": False, "error": str(e)}))
            else:
                print(f"Error: {e}", file=sys.stderr)
            return False


def get_prompt(args, parser) -> str:
    """Get prompt from args or stdin."""
    if args.prompt:
        return args.prompt

    if not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
        if prompt:
            return prompt

    # No prompt provided - show help
    parser.print_help()
    sys.exit(0)


def main():
    config = get_config()

    parser = argparse.ArgumentParser(description="Send prompts to AI providers via browser extension")
    parser.add_argument("prompt", nargs="?", help="The prompt to send")
    parser.add_argument("--port", type=int, default=config["port"], help=f"WebSocket port (default: {config['port']})")
    parser.add_argument("--connection-timeout", type=float, default=config["connection_timeout"], 
                       help=f"Connection timeout in seconds (default: {config['connection_timeout']})")
    parser.add_argument("--response-timeout", type=float, default=config["response_timeout"],
                       help=f"Response timeout in seconds (default: {config['response_timeout']})")
    parser.add_argument("--session", "-s", type=str, help="Session ID to continue a conversation")
    parser.add_argument("--provider", "-p", type=str, choices=["chatgpt", "claude", "grok", "deepseek"], 
                       default=config["default_provider"] or None, help="AI provider to use")
    parser.add_argument("--json", "-j", action="store_true", help="Output response as JSON (includes session_id)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show connection status messages")
    parser.add_argument("--log", action="store_true", default=config["log_enabled"], help="Enable WebSocket message logging")
    parser.add_argument("--no-log", action="store_true", help="Disable WebSocket message logging")
    parser.add_argument("--attach", "-a", type=str, action="append", metavar="FILE", 
                       help="Attach file(s) to the prompt (can be used multiple times)")

    args = parser.parse_args()
    prompt = get_prompt(args, parser)

    # Process file attachments
    attachments = None
    if args.attach:
        attachments = []
        for file_path in args.attach:
            try:
                attachment = read_file_as_attachment(file_path)
                attachments.append(attachment)
            except FileNotFoundError as e:
                print(f"Error: {e}", file=sys.stderr)
                sys.exit(1)

    # Set up WebSocket logger if enabled
    log_enabled = args.log and not args.no_log
    logger = setup_ws_logger(config["log_dir"]) if log_enabled else None

    # Set up queue manager
    queue_dir = config["queue_dir"]
    max_queue_size = config["max_queue_size"]
    queue_manager = QueueManager(queue_dir, max_queue_size)

    # Try to acquire lock
    is_server = queue_manager.try_acquire_lock()

    try:
        cli = ChatGPTCLI(
            port=args.port,
            connection_timeout=args.connection_timeout,
            response_timeout=args.response_timeout,
            verbose=args.verbose,
            ws_logger=logger,
            queue_manager=queue_manager
        )

        if is_server:
            # Run as server
            success = asyncio.run(cli.run_as_server(prompt, args.session, args.provider, args.json, attachments))
        else:
            # Run as queued client
            success = asyncio.run(cli.run_as_client(prompt, args.session, args.provider, args.json, attachments))

        sys.exit(0 if success else 1)

    finally:
        if is_server:
            queue_manager.release_lock()


if __name__ == "__main__":
    main()