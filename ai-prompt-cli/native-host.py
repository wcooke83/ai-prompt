#!/usr/bin/env python3
"""
Native messaging host for AI Provider Automator extension.
Communicates with Firefox extension via stdin/stdout using native messaging protocol.
CLI communicates with this host via a Unix socket.
"""

import asyncio
import json
import os
import struct
import sys
import tempfile

SOCKET_PATH = os.path.join(tempfile.gettempdir(), 'ai-prompt-native.sock')

# Pending requests from CLI waiting for extension responses
pending_requests = {}
extension_connected = False


def send_message_to_extension(message):
    """Send a message to the extension via stdout."""
    encoded = json.dumps(message).encode('utf-8')
    # Native messaging uses 4-byte length prefix (little-endian)
    sys.stdout.buffer.write(struct.pack('<I', len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def read_message_from_extension():
    """Read a message from the extension via stdin."""
    # Read 4-byte length prefix
    length_bytes = sys.stdin.buffer.read(4)
    if not length_bytes:
        return None
    length = struct.unpack('<I', length_bytes)[0]
    # Read the message
    message_bytes = sys.stdin.buffer.read(length)
    return json.loads(message_bytes.decode('utf-8'))


async def handle_cli_client(reader, writer):
    """Handle a CLI client connection."""
    global extension_connected

    try:
        while True:
            # Read length prefix (4 bytes)
            length_bytes = await reader.readexactly(4)
            length = struct.unpack('<I', length_bytes)[0]

            # Read message
            message_bytes = await reader.readexactly(length)
            message = json.loads(message_bytes.decode('utf-8'))

            request_id = message.get('request_id')

            if message.get('type') == 'status':
                # Return connection status
                response = {
                    'type': 'status',
                    'request_id': request_id,
                    'connected': extension_connected
                }
            else:
                # Forward to extension and wait for response
                if not extension_connected:
                    response = {
                        'type': 'response',
                        'request_id': request_id,
                        'success': False,
                        'error': 'Extension not connected'
                    }
                else:
                    # Create a future to wait for the response
                    future = asyncio.get_event_loop().create_future()
                    pending_requests[request_id] = future

                    # Send to extension
                    send_message_to_extension(message)

                    try:
                        # Wait for response (with timeout)
                        response = await asyncio.wait_for(future, timeout=message.get('timeout', 120))
                    except asyncio.TimeoutError:
                        response = {
                            'type': 'response',
                            'request_id': request_id,
                            'success': False,
                            'error': 'Timeout waiting for extension response'
                        }
                    finally:
                        pending_requests.pop(request_id, None)

            # Send response back to CLI
            response_bytes = json.dumps(response).encode('utf-8')
            writer.write(struct.pack('<I', len(response_bytes)))
            writer.write(response_bytes)
            await writer.drain()

    except asyncio.IncompleteReadError:
        pass
    except Exception as e:
        sys.stderr.write(f"CLI handler error: {e}\n")
    finally:
        writer.close()
        await writer.wait_closed()


async def read_extension_messages():
    """Read messages from extension (stdin) in a separate thread."""
    global extension_connected

    loop = asyncio.get_event_loop()

    while True:
        try:
            # Run blocking read in executor
            message = await loop.run_in_executor(None, read_message_from_extension)

            if message is None:
                sys.stderr.write("Extension disconnected\n")
                extension_connected = False
                break

            if message.get('type') == 'ready':
                extension_connected = True
                sys.stderr.write("Extension connected\n")
            elif message.get('type') == 'response':
                request_id = message.get('request_id')
                if request_id in pending_requests:
                    pending_requests[request_id].set_result(message)

        except Exception as e:
            sys.stderr.write(f"Extension read error: {e}\n")
            extension_connected = False
            break


async def main():
    # Remove existing socket
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    # Start Unix socket server for CLI connections
    server = await asyncio.start_unix_server(handle_cli_client, SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)

    sys.stderr.write(f"Native host listening on {SOCKET_PATH}\n")

    # Tell extension we're ready
    send_message_to_extension({'type': 'host_ready'})

    # Start reading from extension
    extension_task = asyncio.create_task(read_extension_messages())

    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        server.close()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
