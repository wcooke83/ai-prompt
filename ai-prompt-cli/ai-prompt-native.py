#!/usr/bin/env python3
"""CLI tool to send prompts to AI providers via native messaging."""

import argparse
import asyncio
import json
import os
import struct
import sys
import tempfile
import uuid

SOCKET_PATH = os.path.join(tempfile.gettempdir(), 'ai-prompt-native.sock')


async def send_and_receive(message, timeout=120):
    """Send a message to native host and wait for response."""
    try:
        reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError):
        return {'success': False, 'error': 'Native host not running. Is Firefox open with the extension?'}

    try:
        # Send message with length prefix
        encoded = json.dumps(message).encode('utf-8')
        writer.write(struct.pack('<I', len(encoded)))
        writer.write(encoded)
        await writer.drain()

        # Read response
        length_bytes = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        length = struct.unpack('<I', length_bytes)[0]
        response_bytes = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)

        return json.loads(response_bytes.decode('utf-8'))

    except asyncio.TimeoutError:
        return {'success': False, 'error': f'Timeout after {timeout}s'}
    finally:
        writer.close()
        await writer.wait_closed()


async def main_async(args, prompt):
    """Main async logic."""
    request_id = str(uuid.uuid4())

    message = {
        'type': 'prompt',
        'request_id': request_id,
        'text': prompt,
        'timeout': args.timeout
    }

    if args.session:
        message['session_id'] = args.session
    elif not args.json:
        message['ephemeral'] = True

    if args.provider:
        message['provider'] = args.provider

    response = await send_and_receive(message, args.timeout)

    if args.json:
        output = {
            'success': response.get('success', False),
            'text': response.get('text', ''),
            'session_id': response.get('session_id'),
            'provider': response.get('provider')
        }
        if response.get('error'):
            output['error'] = response['error']
        print(json.dumps(output))
    else:
        if response.get('success'):
            print(response.get('text', ''))
        else:
            print(f"Error: {response.get('error', 'Unknown error')}", file=sys.stderr)
            sys.exit(1)


def get_prompt(args, parser):
    """Get prompt from args or stdin."""
    if args.prompt:
        return args.prompt

    if not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
        if prompt:
            return prompt

    parser.print_help()
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Send prompts to AI providers via native messaging")
    parser.add_argument("prompt", nargs="?", help="The prompt to send")
    parser.add_argument("--timeout", type=float, default=120, help="Timeout in seconds (default: 120)")
    parser.add_argument("--session", "-s", type=str, help="Session ID to continue a conversation")
    parser.add_argument("--provider", "-p", type=str, choices=["chatgpt", "claude", "grok", "deepseek"],
                        help="AI provider to use")
    parser.add_argument("--json", "-j", action="store_true",
                        help="Output response as JSON (includes session_id)")
    parser.add_argument("--status", action="store_true", help="Check native host status")

    args = parser.parse_args()

    if args.status:
        response = asyncio.run(send_and_receive({'type': 'status', 'request_id': 'status'}))
        if response.get('connected'):
            print("Native host: connected to extension")
        else:
            print(f"Native host: {response.get('error', 'not connected')}")
        sys.exit(0)

    prompt = get_prompt(args, parser)
    asyncio.run(main_async(args, prompt))


if __name__ == "__main__":
    main()
