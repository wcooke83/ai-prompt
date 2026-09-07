"""The routing core: pick an upstream, forward, fail over, remember who owns what.

Everything a front can ask for lives here, so the HTTP app, the WebSocket front and
the unix-socket front all share one implementation of the rules in plan section 5.

Two exceptions are part of the interface because they map onto HTTP status codes the
caller already understands:

  LoopDetected        -> 508, a request came back to a router it had already visited
  NoUpstreamAvailable -> 503 "Browser extension not connected", the exact detail a
                         single bridge returns when its extension is gone, so callers
                         written against a bridge need no change.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import httpx
from pydantic import ValidationError

from ai_prompt_api.models import (
    PromptRequest,
    PromptResponse,
    Provider,
    SessionInfo,
    SessionListResponse,
    StatusResponse,
)

from .config import RouterConfig, UpstreamConfig
from .sessions import SessionMap
from .strategy import make_strategy
from .upstreams import UpstreamClient

logger = logging.getLogger(__name__)

# How long after a transport failure we re-probe an upstream. Short, because the
# usual cause is a browser that was restarted, and the alternative is waiting out a
# whole health_interval with that upstream out of rotation.
RECHECK_DELAY_SECONDS = 2.0

HOPS_HEADER = "X-AI-Prompt-Hops"
VIA_HEADER = "X-AI-Prompt-Via"

# The bridge lost the extension before sending. Retryable, and it also tells us the
# upstream is down right now, so we mark it as well.
EXTENSION_GONE_MARKER = "extension not connected"

# Substrings in a 200 `success:false` error that mean this browser could not offer the
# requested provider at all, so nothing was typed and nothing was sent -- a retry on the
# next upstream cannot double-send. The provider itself never changes; only which
# browser we ask, which is what keeps the bridge's "true pin" promise intact.
#
# These are matched case-insensitively against the exact strings background.js emits,
# and they are deliberately narrow. In particular, match "is disabled" / "are disabled"
# and NEVER the bare word "disabled": content.js throws "Send button not found or
# remained disabled" *after* the prompt has been typed into the page, and treating that
# as retryable would risk sending the same prompt twice.
PROVIDER_UNAVAILABLE_MARKERS = (
    "not available",               # background.js:574 - no selectors for it in this build
    "is disabled",                 # background.js:729 - pinned provider switched off in that popup
    "all providers are disabled",  # background.js:764 - every provider switched off in that browser
)


class LoopDetected(Exception):
    """A request has visited this router before (or exhausted max_hops)."""


class NoUpstreamAvailable(Exception):
    """Nothing eligible to send to, or nothing that ever reached a bridge."""


@dataclass
class ForwardResult:
    response: PromptResponse
    upstream: Optional[str]
    attempts: list[str] = field(default_factory=list)


@dataclass
class _Attempt:
    """What one upstream did with the prompt."""

    retry: bool
    response: Optional[PromptResponse] = None
    error: Optional[str] = None


class Router:
    def __init__(
        self,
        config: RouterConfig,
        *,
        transport_factory=None,
    ) -> None:
        self.config = config
        # Stable for the life of the process and carried in X-AI-Prompt-Via. A fresh
        # id per restart is fine: Via only has to detect a cycle inside one in-flight
        # request, never across restarts.
        self.instance_id = f"router-{uuid.uuid4().hex[:8]}"
        self.strategy = make_strategy(config.strategy)
        self.sessions = SessionMap(config.sessions_path, config.session_ttl_hours)

        # transport_factory(upstream_config) -> httpx transport | None. Test seam only
        # (ASGI transports let the suite run a bridge and a second router in-process).
        self._transport_factory = transport_factory
        self.upstreams: list[UpstreamClient] = [
            UpstreamClient(
                upstream,
                default_timeout=config.default_timeout,
                transport=self._make_transport(upstream),
            )
            for upstream in config.upstreams
        ]
        self._by_name = {upstream.name: upstream for upstream in self.upstreams}

        self._health_task: Optional[asyncio.Task] = None
        self._recheck_tasks: set[asyncio.Task] = set()
        self._recheck_pending: set[str] = set()
        self._started = False

    def _make_transport(self, upstream: UpstreamConfig):
        if self._transport_factory is None:
            return None
        return self._transport_factory(upstream)

    # --- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        for upstream in self.upstreams:
            upstream.open()
        self.sessions.load()
        # One sweep before serving traffic so the very first request routes on facts
        # rather than on the optimistic default in UpstreamClient.
        await self._health_sweep()
        self._health_task = asyncio.create_task(self._health_loop(), name="router-health")
        self._started = True
        logger.info(
            "router %s started with %d upstream(s): %s",
            self.instance_id,
            len(self.upstreams),
            ", ".join(u.name for u in self.upstreams),
        )

    async def stop(self) -> None:
        self._started = False
        tasks = [t for t in [self._health_task, *self._recheck_tasks] if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - shutdown is best-effort
                pass
        self._health_task = None
        self._recheck_tasks.clear()
        self._recheck_pending.clear()
        for upstream in self.upstreams:
            await upstream.aclose()

    # --- health ---------------------------------------------------------------

    async def _health_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.config.health_interval_seconds)
                await self._health_sweep()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the poller must never die
                logger.exception("health sweep failed")

    async def _health_sweep(self) -> None:
        targets = [
            u
            for u in self.upstreams
            # A misconfigured upstream is skipped on purpose: the bridge's /health is
            # unauthenticated, so polling it would report "ok" and overwrite the 401
            # explanation an operator needs to see in /upstreams.
            if u.enabled and not u.misconfigured
        ]
        if targets:
            await asyncio.gather(*(u.check_health() for u in targets), return_exceptions=True)

    def _mark_down(self, upstream: UpstreamClient, error: str) -> None:
        """Passive marking: a failure during a real request is the freshest signal."""
        upstream.mark_unhealthy(error)
        self._schedule_recheck(upstream)

    def _schedule_recheck(self, upstream: UpstreamClient) -> None:
        if upstream.name in self._recheck_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - only outside an event loop
            return
        self._recheck_pending.add(upstream.name)
        task = loop.create_task(self._recheck_later(upstream), name=f"recheck-{upstream.name}")
        self._recheck_tasks.add(task)
        task.add_done_callback(self._recheck_tasks.discard)

    async def _recheck_later(self, upstream: UpstreamClient) -> None:
        try:
            await asyncio.sleep(RECHECK_DELAY_SECONDS)
            await upstream.check_health()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("recheck of upstream %s failed", upstream.name)
        finally:
            self._recheck_pending.discard(upstream.name)

    def any_healthy(self) -> bool:
        return any(u.is_eligible() for u in self.upstreams)

    # --- selection ------------------------------------------------------------

    def eligible(self, provider: Optional[str] = None) -> list[UpstreamClient]:
        """enabled AND healthy AND (if pinned) lists that provider (plan 5.1 step 3)."""
        return [u for u in self.upstreams if u.is_eligible(provider)]

    def _attempt_order(self, provider: Optional[str]) -> list[UpstreamClient]:
        candidates = self.eligible(provider)
        if not candidates:
            return []
        order = self.strategy.order(candidates)
        # failover off = one shot; otherwise the rotation is truncated to max_attempts
        # so a request can never walk a long upstream list one dead bridge at a time.
        limit = self.config.max_attempts if self.config.failover else 1
        return order[:limit]

    # --- forwarding -----------------------------------------------------------

    async def forward_prompt(
        self,
        request: PromptRequest,
        *,
        hops: int = 0,
        via: Optional[list[str]] = None,
    ) -> ForwardResult:
        chain = list(via or [])

        # 1. Hop guard. Via catches a genuine cycle exactly; max_hops catches a long
        #    chain (or one built out of routers too old to send Via at all).
        if self.instance_id in chain:
            raise LoopDetected(
                f"request already passed through this router ({self.instance_id}); via={chain}"
            )
        if hops >= self.config.max_hops:
            raise LoopDetected(f"max_hops ({self.config.max_hops}) exceeded; via={chain}")

        provider = request.provider.value if request.provider else None

        # 2. Session affinity. The tab lives on one browser, so nothing else can
        #    continue the conversation -- no rotation and no failover for these.
        pinned_owner: Optional[UpstreamClient] = None
        if request.session_id:
            pinned_owner = await self._owner_for(request.session_id)

        if pinned_owner is not None:
            order: list[UpstreamClient] = [pinned_owner]
        else:
            # 3 + 4. Eligibility filter, then round-robin over what is left.
            order = self._attempt_order(provider)
            if not order:
                raise NoUpstreamAvailable(
                    "no eligible upstream"
                    + (f" for provider {provider!r}" if provider else "")
                )

        payload = request.model_dump(mode="json", exclude_none=True)
        headers = {
            HOPS_HEADER: str(hops + 1),
            VIA_HEADER: ",".join(chain + [self.instance_id]),
        }

        attempts: list[str] = []
        last_response: Optional[PromptResponse] = None
        last_upstream: Optional[str] = None
        last_error: Optional[str] = None

        for upstream in order:
            attempts.append(upstream.name)
            outcome = await self._attempt(upstream, request, payload, headers)

            if outcome.response is not None:
                last_response = outcome.response
                last_upstream = upstream.name
            if outcome.error:
                last_error = outcome.error

            if not outcome.retry:
                self._remember_session(request, outcome.response, upstream)
                return ForwardResult(
                    response=outcome.response, upstream=upstream.name, attempts=attempts
                )

            if pinned_owner is not None:
                # A known session owner never fails over (plan 5.2). If it managed to
                # answer at all, that answer is the truth about this conversation.
                if outcome.response is not None:
                    return ForwardResult(
                        response=outcome.response, upstream=upstream.name, attempts=attempts
                    )
                break

        # Exhausted. If some upstream produced a real body (e.g. every browser said
        # the pinned provider is "not available"), return it -- the bridge's own words
        # are more useful than a synthesised 503. If nothing ever reached a bridge,
        # this is indistinguishable from "extension not connected" to the caller.
        if last_response is not None:
            return ForwardResult(response=last_response, upstream=last_upstream, attempts=attempts)
        raise NoUpstreamAvailable(last_error or "no upstream answered")

    async def _attempt(
        self,
        upstream: UpstreamClient,
        request: PromptRequest,
        payload: dict,
        headers: dict,
    ) -> _Attempt:
        """Send one prompt and classify the result against the plan 5.2 table."""
        try:
            with upstream.track_in_flight():
                response = await upstream.post_prompt(
                    payload, headers=headers, request_timeout=request.timeout
                )
        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            # We waited out the upstream's own timeout plus the grace and still got
            # nothing. The prompt may well be running in that browser right now, so
            # this is the one transport failure that must NOT be retried.
            message = f"upstream {upstream.name} did not respond in time ({exc})"
            logger.warning(message)
            return _Attempt(
                retry=False,
                response=PromptResponse(success=False, error=message),
                error=message,
            )
        except httpx.HTTPError as exc:
            # Connection refused / reset / DNS / pool exhaustion: the request never
            # reached the bridge, so another upstream is safe to try.
            message = f"{type(exc).__name__}: {exc}"
            self._mark_down(upstream, message)
            return _Attempt(retry=True, error=f"upstream {upstream.name}: {message}")

        status = response.status_code

        if status == 508:
            # A downstream router detected the loop for us; surfacing anything else
            # here (like a 503 after retries) would hide the real cause.
            raise LoopDetected(f"upstream {upstream.name} reported a routing loop")

        if status in (401, 403):
            message = f"HTTP {status} from {upstream.name} (check its api_key in router.json)"
            upstream.mark_misconfigured(message)
            return _Attempt(retry=True, error=message)

        if status == 503:
            # The bridge refused before sending anything to a browser.
            message = f"HTTP 503 from {upstream.name} (extension not connected)"
            self._mark_down(upstream, message)
            return _Attempt(retry=True, error=message)

        if status >= 400:
            # 404/422/500 and friends are not in the failover table: they are either
            # our own bad request (every upstream would reject it identically) or a
            # failure we cannot prove was harmless.
            message = f"HTTP {status} from {upstream.name}: {response.text[:200]}"
            logger.warning(message)
            return _Attempt(
                retry=False, response=PromptResponse(success=False, error=message), error=message
            )

        try:
            parsed = PromptResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            message = f"upstream {upstream.name} returned an unreadable body: {exc}"
            logger.warning(message)
            return _Attempt(
                retry=False, response=PromptResponse(success=False, error=message), error=message
            )

        # The upstream answered, so it is reachable and our key for it is good.
        upstream.misconfigured = False
        upstream.mark_healthy()

        if parsed.success:
            return _Attempt(retry=False, response=parsed)

        error_text = (parsed.error or "").lower()
        if EXTENSION_GONE_MARKER in error_text:
            self._mark_down(upstream, parsed.error or EXTENSION_GONE_MARKER)
            return _Attempt(retry=True, response=parsed, error=parsed.error)
        if any(marker in error_text for marker in PROVIDER_UNAVAILABLE_MARKERS):
            # This browser cannot offer that provider (missing, or switched off in its
            # popup). Nothing was sent, so ask the next browser for the SAME provider.
            # The upstream stays healthy -- it is fine, it just does not have this one.
            return _Attempt(retry=True, response=parsed, error=parsed.error)

        # "Timeout after Ns" and every other error: the provider ran and failed (or
        # may still be running). Return it as-is.
        return _Attempt(retry=False, response=parsed, error=parsed.error)

    def _remember_session(
        self,
        request: PromptRequest,
        response: Optional[PromptResponse],
        upstream: UpstreamClient,
    ) -> None:
        if response is None or not response.success or not response.session_id:
            return
        self.sessions.record(response.session_id, upstream.name, response.provider)

    # --- session ownership ----------------------------------------------------

    async def _owner_for(self, session_id: str) -> Optional[UpstreamClient]:
        """Map first, then fan out. None means "treat it as a new conversation".

        Falling back to a new conversation matches what the extension itself does with
        an unknown session id, so a router restart that lost its map degrades the same
        way the browser already does rather than erroring.
        """
        name = self.sessions.owner(session_id)
        if name:
            upstream = self._by_name.get(name)
            if upstream is not None:
                return upstream
            # The upstream was renamed or removed from the config; the mapping is dead.
            logger.info(
                "session %s was owned by unknown upstream %s; forgetting", session_id, name
            )
            self.sessions.forget(session_id)

        upstream, _info = await self._fan_out_session(session_id)
        return upstream

    async def _fan_out_session(
        self, session_id: str
    ) -> tuple[Optional[UpstreamClient], Optional[SessionInfo]]:
        """Ask every enabled upstream who owns a session; first 200 in config order wins.

        Unhealthy upstreams are included deliberately: "unhealthy" here means the
        browser has no extension attached right now, but the bridge process still
        remembers the session and may get its extension back.
        """
        targets = [u for u in self.upstreams if u.enabled]
        if not targets:
            return None, None

        results = await asyncio.gather(
            *(self._fetch_session(u, session_id) for u in targets), return_exceptions=True
        )
        for upstream, info in zip(targets, results):
            if isinstance(info, BaseException) or info is None:
                continue
            self.sessions.record(session_id, upstream.name, info.provider, info.created_at)
            return upstream, info
        return None, None

    async def _fetch_session(
        self, upstream: UpstreamClient, session_id: str
    ) -> Optional[SessionInfo]:
        try:
            response = await upstream.get_session(session_id)
        except httpx.HTTPError as exc:
            self._mark_down(upstream, f"{type(exc).__name__}: {exc}")
            return None
        if response.status_code != 200:
            return None
        try:
            return SessionInfo.model_validate(response.json())
        except (ValueError, ValidationError):
            return None

    # --- session endpoints ----------------------------------------------------

    async def list_sessions(self) -> list[SessionInfo]:
        targets = [u for u in self.upstreams if u.enabled and u.healthy]
        if not targets:
            return []
        results = await asyncio.gather(
            *(self._list_sessions_on(u) for u in targets), return_exceptions=True
        )

        merged: dict[str, SessionInfo] = {}
        for upstream, infos in zip(targets, results):
            if isinstance(infos, BaseException):
                continue
            for info in infos:
                if info.session_id in merged:
                    continue
                merged[info.session_id] = info
                # Free ownership discovery: a listing tells us where sessions we have
                # never seen live, which spares a later fan-out on the first follow-up.
                if self.sessions.owner(info.session_id) is None:
                    self.sessions.record(
                        info.session_id, upstream.name, info.provider, info.created_at
                    )
        return list(merged.values())

    async def _list_sessions_on(self, upstream: UpstreamClient) -> list[SessionInfo]:
        try:
            response = await upstream.list_sessions()
        except httpx.HTTPError as exc:
            self._mark_down(upstream, f"{type(exc).__name__}: {exc}")
            return []
        if response.status_code != 200:
            return []
        try:
            return SessionListResponse.model_validate(response.json()).sessions
        except (ValueError, ValidationError):
            return []

    async def get_session(self, session_id: str) -> Optional[SessionInfo]:
        name = self.sessions.owner(session_id)
        if name:
            upstream = self._by_name.get(name)
            if upstream is not None:
                info = await self._fetch_session(upstream, session_id)
                if info is not None:
                    return info
                # 404 from the recorded owner: the tab is gone, so the mapping is
                # stale and must not keep pinning follow-ups at a dead browser.
                self.sessions.forget(session_id)
            else:
                self.sessions.forget(session_id)

        _upstream, info = await self._fan_out_session(session_id)
        return info

    async def delete_session(self, session_id: str) -> bool:
        upstream = await self._owner_for(session_id)
        if upstream is None:
            self.sessions.forget(session_id)
            return False
        try:
            response = await upstream.delete_session(session_id)
        except httpx.HTTPError as exc:
            self._mark_down(upstream, f"{type(exc).__name__}: {exc}")
            return False

        if response.status_code == 200:
            self.sessions.forget(session_id)
            return True
        if response.status_code == 404:
            self.sessions.forget(session_id)
        return False

    # --- visibility -----------------------------------------------------------

    def available_providers(self) -> list[str]:
        enabled = [u for u in self.upstreams if u.enabled]
        # An upstream with no `providers` list means "assume every provider", so one
        # such upstream makes the union the whole enum.
        if any(u.providers is None for u in enabled) or not enabled:
            return [p.value for p in Provider]
        union: set[str] = set()
        for upstream in enabled:
            union.update(upstream.providers or [])
        return sorted(union)

    def status_snapshot(self) -> StatusResponse:
        return StatusResponse(
            extension_connected=self.any_healthy(),
            active_provider=None,
            available_providers=self.available_providers(),
            pending_requests=sum(u.in_flight for u in self.upstreams),
            active_sessions=len(self.sessions),
        )

    def upstreams_snapshot(self) -> list[dict[str, Any]]:
        return [u.snapshot(self.sessions.owned_by(u.name)) for u in self.upstreams]


def parse_hops(value: Optional[str]) -> int:
    """X-AI-Prompt-Hops from an untrusted caller; anything odd counts as 0."""
    try:
        hops = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0
    return max(hops, 0)


def parse_via(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def format_via(chain: Sequence[str]) -> str:
    return ",".join(chain)
