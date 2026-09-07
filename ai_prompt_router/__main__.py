"""`python -m ai_prompt_router` -- load the config, wire the fronts, serve.

Startup order matters: the Router must be reachable through `state` before any front
accepts a caller, and the fronts must be stopped before the router's httpx clients
close, or an in-flight prompt would fail on a closed client during shutdown. Both are
handled by the lifespan below rather than by ad-hoc calls, so uvicorn's signal
handling drives the teardown.
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import asynccontextmanager

import uvicorn

from . import state
from .app import create_app
from .config import RouterConfig, load_config
from .router import Router

logger = logging.getLogger("ai_prompt_router")


async def _start_fronts(config: RouterConfig, router: Router) -> None:
    """Start the optional WebSocket / unix-socket fronts.

    Imported lazily and guarded: an ImportError here (a missing `websockets`, or a
    front module that is not installed in this deployment) must degrade to "HTTP only"
    rather than take the whole router down -- the HTTP front is the one apps use.
    """
    if config.ws_targets:
        try:
            from .fronts.ws_client import WebSocketFront
        except ImportError as exc:
            logger.warning("WebSocket front unavailable (%s); ws_targets ignored", exc)
        else:
            front = WebSocketFront(
                [target.url for target in config.ws_targets], router, config.default_timeout
            )
            await front.start()
            state.ws_front = front

    if config.unix_socket is not None:
        try:
            from .fronts.unix_socket import UnixSocketFront
        except ImportError as exc:
            logger.warning("unix-socket front unavailable (%s); socket disabled", exc)
        else:
            front = UnixSocketFront(config.unix_socket.path, router)
            await front.start()
            state.unix_front = front


async def _stop_fronts() -> None:
    for attr in ("ws_front", "unix_front"):
        front = getattr(state, attr, None)
        if front is None:
            continue
        try:
            await front.stop()
        except Exception:  # noqa: BLE001 - shutdown is best-effort
            logger.exception("error stopping %s", attr)
        setattr(state, attr, None)


def build_app(config: RouterConfig):
    @asynccontextmanager
    async def lifespan(app):
        router = Router(config)
        await router.start()
        state.router = router
        app.state.router = router
        await _start_fronts(config, router)
        try:
            yield
        finally:
            await _stop_fronts()
            await router.stop()
            state.router = None

    return create_app(lifespan=lifespan)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ai_prompt_router",
        description="Round-robin router in front of several ai-prompt bridges",
    )
    parser.add_argument("-c", "--config", help="path to router.json (default: ~/.ai-prompt/router.json)")
    parser.add_argument("-p", "--port", type=int, help="override listen.port")
    parser.add_argument("--host", help="override listen.host")
    parser.add_argument("--log-level", default="info", help="uvicorn log level (default: info)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except ValueError as exc:
        # Config problems are operator errors, not crashes: one clear line on stderr
        # beats a pydantic traceback in a systemd journal.
        print(f"ai-prompt-router: {exc}", file=sys.stderr)
        return 2

    if args.port is not None:
        config.listen.port = args.port
    if args.host is not None:
        config.listen.host = args.host

    uvicorn.run(
        build_app(config),
        host=config.listen.host,
        port=config.listen.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
