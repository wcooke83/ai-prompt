"""One HTTP client + health state per configured upstream.

An upstream is either a bridge (ai_prompt_api) or another router; both speak the
identical four-route contract, so nothing here needs to know which it is.

Timeout policy (plan section 5.1 step 5) is the subtle part. The read timeout is the
caller's own timeout plus a 15 s grace, so that when a prompt runs long it is the
*bridge* that gives up first and answers `success:false, "Timeout after Ns"`. If the
router timed out first the caller would get a router-shaped error instead of the
bridge's, and -- worse -- the router could not tell whether the prompt had run, which
is exactly the case where a retry would double-send.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import httpx

from .config import UpstreamConfig

logger = logging.getLogger(__name__)

# Connecting to a bridge on the same box (or a LAN peer) is either instant or never;
# 5 s is generous enough for a loaded box and short enough that a dead upstream costs
# almost nothing before failover moves on.
CONNECT_TIMEOUT = 5.0
READ_GRACE = 15.0
HEALTH_TIMEOUT = 5.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UpstreamClient:
    def __init__(
        self,
        config: UpstreamConfig,
        *,
        default_timeout: float = 120.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = config
        self.default_timeout = default_timeout
        # Test seam: the integration tests hand in an ASGI/mock transport so a whole
        # bridge (or another router) can be exercised without binding a port.
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None

        # Optimistic until proven otherwise: the first health sweep only completes a
        # moment after start(), and refusing every request until then would make a
        # restart look like an outage. A dead upstream is caught by the first real
        # request anyway (passive marking) and then skipped.
        self.healthy = True
        self.last_check: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.in_flight = 0
        # Sticky: set when an upstream answers 401/403, i.e. *our* key for it is
        # wrong. It is deliberately not cleared by the health poller, because the
        # bridge's /health is unauthenticated and would happily keep saying "ok"
        # while every prompt we send is rejected. Fixing the key means editing
        # router.json, which means a restart anyway.
        self.misconfigured = False

    # --- identity -------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def url(self) -> str:
        return self.config.url

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def providers(self) -> Optional[list[str]]:
        return self.config.providers

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<UpstreamClient {self.name} {self.url} healthy={self.healthy}>"

    # --- lifecycle ------------------------------------------------------------

    def open(self) -> None:
        if self._client is not None:
            return
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.url,
            headers=headers,
            transport=self._transport,
            timeout=self.timeout_for(None),
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(f"upstream {self.name!r} used before open()")
        return self._client

    # --- eligibility ----------------------------------------------------------

    def supports(self, provider: Optional[str]) -> bool:
        """A missing `providers` list means "assume every provider" (plan section 4)."""
        if provider is None or self.providers is None:
            return True
        return provider in self.providers

    def is_eligible(self, provider: Optional[str] = None) -> bool:
        return self.enabled and self.healthy and not self.misconfigured and self.supports(provider)

    def timeout_for(self, request_timeout: Optional[float]) -> httpx.Timeout:
        read = (request_timeout or self.default_timeout) + READ_GRACE
        # write gets the same budget as read: attachments are base64 inside the JSON
        # body and a big one can take real time to push to a remote upstream.
        return httpx.Timeout(read, connect=CONNECT_TIMEOUT, write=read, pool=CONNECT_TIMEOUT)

    # --- health ---------------------------------------------------------------

    def mark_healthy(self) -> None:
        self.healthy = True
        self.last_error = None
        self.last_check = _utcnow()

    def mark_unhealthy(self, error: str) -> None:
        if self.healthy:
            logger.warning("upstream %s marked unhealthy: %s", self.name, error)
        self.healthy = False
        self.last_error = error
        self.last_check = _utcnow()

    def mark_misconfigured(self, error: str) -> None:
        logger.error("upstream %s rejected our API key: %s", self.name, error)
        self.misconfigured = True
        self.last_error = error
        self.last_check = _utcnow()

    async def check_health(self) -> bool:
        """Healthy = HTTP 200 AND extension_connected:true.

        A router upstream reports `extension_connected = any of its upstreams
        healthy`, so chains compose without a separate probe.
        """
        try:
            response = await self.client.get("/health", timeout=httpx.Timeout(HEALTH_TIMEOUT))
        except httpx.HTTPError as exc:
            self.mark_unhealthy(f"{type(exc).__name__}: {exc}")
            return False

        if response.status_code != 200:
            self.mark_unhealthy(f"/health returned HTTP {response.status_code}")
            return False

        try:
            connected = bool(response.json().get("extension_connected"))
        except ValueError:
            self.mark_unhealthy("/health returned non-JSON")
            return False

        if not connected:
            self.mark_unhealthy("extension not connected")
            return False

        self.mark_healthy()
        return True

    # --- requests -------------------------------------------------------------

    @contextlib.contextmanager
    def track_in_flight(self) -> Iterator[None]:
        self.in_flight += 1
        try:
            yield
        finally:
            self.in_flight -= 1

    async def post_prompt(
        self,
        payload: dict,
        *,
        headers: Optional[dict] = None,
        request_timeout: Optional[float] = None,
    ) -> httpx.Response:
        return await self.client.post(
            "/prompt",
            json=payload,
            headers=headers,
            timeout=self.timeout_for(request_timeout),
        )

    async def list_sessions(self) -> httpx.Response:
        return await self.client.get("/sessions", timeout=httpx.Timeout(HEALTH_TIMEOUT))

    async def get_session(self, session_id: str) -> httpx.Response:
        return await self.client.get(
            f"/sessions/{session_id}", timeout=httpx.Timeout(HEALTH_TIMEOUT)
        )

    async def delete_session(self, session_id: str) -> httpx.Response:
        return await self.client.delete(
            f"/sessions/{session_id}", timeout=httpx.Timeout(HEALTH_TIMEOUT)
        )

    # --- visibility -----------------------------------------------------------

    def snapshot(self, sessions_owned: int = 0) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "enabled": self.enabled,
            "healthy": self.healthy,
            "misconfigured": self.misconfigured,
            "providers": self.providers,
            "last_check": self.last_check.isoformat() if self.last_check else None,
            "last_error": self.last_error,
            "in_flight": self.in_flight,
            "sessions_owned": sessions_owned,
        }
