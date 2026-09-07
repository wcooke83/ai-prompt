"""Phase 3: the failover table, health tracking, /upstreams -- plus the end-to-end
integration tests that stand real ai_prompt_api bridges (and a second router) behind
one router over httpx ASGI transports.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from conftest import (
    ScriptedUpstream,
    StubExtension,
    asgi_transport,
    build_router,
    make_bridge_app,
    make_router_app,
    raise_transport,
    respond_failure,
    respond_status,
    router_client,
    router_with,
    running,
)

from ai_prompt_api.models import PromptRequest
from ai_prompt_router.config import RouterConfig, UpstreamConfig
from ai_prompt_router.router import NoUpstreamAvailable, Router
from ai_prompt_router.upstreams import UpstreamClient

# --------------------------------------------------------------------------- #
# The failover table (plan section 5.2), one case per row
# --------------------------------------------------------------------------- #

RETRYABLE_ROWS = [
    pytest.param(raise_transport(httpx.ConnectError("connection refused")), id="connect-refused"),
    pytest.param(raise_transport(httpx.ReadError("connection reset")), id="connection-reset"),
    pytest.param(
        raise_transport(httpx.ConnectError("[Errno 8] nodename nor servname provided")), id="dns"
    ),
    pytest.param(respond_status(503, {"detail": "Browser extension not connected"}), id="http-503"),
    pytest.param(respond_status(401, {"detail": "Invalid or missing API key"}), id="http-401"),
    pytest.param(respond_status(403, {"detail": "forbidden"}), id="http-403"),
    pytest.param(respond_failure("Extension not connected"), id="body-extension-not-connected"),
    pytest.param(respond_failure("Provider grok is not available"), id="body-provider-unavailable"),
    # The strings background.js actually emits when a pinned provider cannot run in that
    # browser. Nothing is typed into a page on any of these paths, so moving to the next
    # browser is safe -- and it is the only thing that makes a pinned provider survive
    # being switched off in one popup.
    pytest.param(respond_failure("Provider deepseek is disabled"), id="body-provider-disabled"),
    pytest.param(respond_failure("All providers are disabled"), id="body-all-providers-disabled"),
]

FINAL_ROWS = [
    pytest.param(respond_failure("Timeout after 120s"), id="body-timeout"),
    pytest.param(respond_failure("ChatGPT returned an empty response"), id="body-other-error"),
    pytest.param(raise_transport(httpx.ReadTimeout("read timed out")), id="read-timeout"),
    # Guards the narrowness of PROVIDER_UNAVAILABLE_MARKERS: content.js throws this
    # AFTER typing the prompt into the page, so matching the bare word "disabled" here
    # would risk sending the same prompt to a second browser.
    pytest.param(
        respond_failure("Send button not found or remained disabled"),
        id="body-send-button-disabled",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", RETRYABLE_ROWS)
async def test_failover_rows_that_never_executed_are_retried(handler, sessions_path):
    bad = ScriptedUpstream("ff1", prompt=handler)
    good = ScriptedUpstream("chrome")

    async with router_with([bad, good], sessions_path) as router:
        result = await router.forward_prompt(PromptRequest(text="hi"))

    assert result.attempts == ["ff1", "chrome"]
    assert result.upstream == "chrome"
    assert result.response.success is True


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", FINAL_ROWS)
async def test_failover_rows_that_may_have_executed_are_not_retried(handler, sessions_path):
    """A retry here could double-send: the prompt may be running in that browser."""
    bad = ScriptedUpstream("ff1", prompt=handler)
    good = ScriptedUpstream("chrome")

    async with router_with([bad, good], sessions_path) as router:
        result = await router.forward_prompt(PromptRequest(text="hi"))

    assert result.attempts == ["ff1"]
    assert result.response.success is False
    assert good.calls["prompt"] == 0


@pytest.mark.asyncio
async def test_success_returns_on_the_first_attempt(sessions_path):
    stubs = [ScriptedUpstream("ff1"), ScriptedUpstream("chrome")]
    async with router_with(stubs, sessions_path) as router:
        result = await router.forward_prompt(PromptRequest(text="hi"))
    assert result.attempts == ["ff1"]
    assert stubs[1].calls["prompt"] == 0


@pytest.mark.asyncio
async def test_a_401_marks_the_upstream_misconfigured_and_takes_it_out_of_rotation(sessions_path):
    bad = ScriptedUpstream("ff1", prompt=respond_status(401))
    good = ScriptedUpstream("chrome")

    async with router_with([bad, good], sessions_path) as router:
        await router.forward_prompt(PromptRequest(text="one"))
        upstream = router._by_name["ff1"]
        assert upstream.misconfigured is True
        assert "401" in (upstream.last_error or "")

        # It stays out even though its /health keeps saying "ok" -- the bridge's
        # /health is unauthenticated, so only a real request can tell us the key is bad.
        await router._health_sweep()
        assert upstream.misconfigured is True
        second = await router.forward_prompt(PromptRequest(text="two"))

    assert second.attempts == ["chrome"]
    assert bad.calls["prompt"] == 1


@pytest.mark.asyncio
async def test_failover_is_bounded_by_max_attempts(sessions_path):
    stubs = [ScriptedUpstream(f"b{i}", prompt=respond_status(503)) for i in range(4)]
    async with router_with(stubs, sessions_path, max_attempts=3) as router:
        with pytest.raises(NoUpstreamAvailable):
            await router.forward_prompt(PromptRequest(text="hi"))

    assert sum(stub.calls["prompt"] for stub in stubs) == 3


@pytest.mark.asyncio
async def test_failover_can_be_switched_off(sessions_path):
    bad = ScriptedUpstream("ff1", prompt=respond_status(503))
    good = ScriptedUpstream("chrome")
    async with router_with([bad, good], sessions_path, failover=False) as router:
        with pytest.raises(NoUpstreamAvailable):
            await router.forward_prompt(PromptRequest(text="hi"))
    assert good.calls["prompt"] == 0


@pytest.mark.asyncio
async def test_exhausted_retries_return_the_last_real_answer(sessions_path):
    """Every browser says the pinned provider is missing: say so, do not invent a 503."""
    stubs = [
        ScriptedUpstream("ff1", prompt=respond_failure("Provider grok is not available")),
        ScriptedUpstream("chrome", prompt=respond_failure("Provider grok is not available")),
    ]
    async with router_with(stubs, sessions_path) as router:
        result = await router.forward_prompt(PromptRequest(text="hi", provider="grok"))

    assert result.attempts == ["ff1", "chrome"]
    assert result.response.success is False
    assert "not available" in result.response.error


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


def make_upstream(handler) -> UpstreamClient:
    upstream = UpstreamClient(
        UpstreamConfig(name="x", url="http://x.test"), transport=httpx.MockTransport(handler)
    )
    upstream.open()
    return upstream


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler, expected_healthy, expected_error",
    [
        (lambda r: httpx.Response(200, json={"status": "ok", "extension_connected": True}), True, None),
        (
            lambda r: httpx.Response(200, json={"status": "ok", "extension_connected": False}),
            False,
            "extension not connected",
        ),
        (lambda r: httpx.Response(500, text="boom"), False, "HTTP 500"),
    ],
)
async def test_health_requires_200_and_extension_connected(handler, expected_healthy, expected_error):
    upstream = make_upstream(handler)
    try:
        assert await upstream.check_health() is expected_healthy
        assert upstream.healthy is expected_healthy
        assert upstream.last_check is not None
        if expected_error:
            assert expected_error in upstream.last_error
    finally:
        await upstream.aclose()


@pytest.mark.asyncio
async def test_health_check_survives_a_dead_upstream():
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    upstream = make_upstream(refuse)
    try:
        assert await upstream.check_health() is False
        assert "ConnectError" in upstream.last_error
    finally:
        await upstream.aclose()


@pytest.mark.asyncio
async def test_health_poller_notices_a_browser_going_away_and_coming_back(sessions_path):
    stub = ScriptedUpstream("ff1")
    async with router_with([stub], sessions_path, health_interval_seconds=0.02) as router:
        upstream = router._by_name["ff1"]
        assert upstream.healthy is True

        stub.connected = False
        await asyncio.sleep(0.1)
        assert upstream.healthy is False
        assert router.any_healthy() is False

        stub.connected = True
        await asyncio.sleep(0.1)
        assert upstream.healthy is True


@pytest.mark.asyncio
async def test_transport_failure_marks_unhealthy_and_schedules_a_recheck(sessions_path, monkeypatch):
    import ai_prompt_router.router as router_module

    # The real delay is 2 s; the behaviour under test is "it comes back on its own".
    monkeypatch.setattr(router_module, "RECHECK_DELAY_SECONDS", 0.01)

    failing = {"now": True}

    def flaky(upstream, request, body):
        if failing["now"]:
            raise httpx.ConnectError("connection refused")
        return upstream.default_prompt(body)

    bad = ScriptedUpstream("ff1", prompt=flaky)
    good = ScriptedUpstream("chrome")

    async with router_with([bad, good], sessions_path, health_interval_seconds=3600) as router:
        upstream = router._by_name["ff1"]
        await router.forward_prompt(PromptRequest(text="hi"))
        assert upstream.healthy is False
        assert [u.name for u in router.eligible()] == ["chrome"]

        failing["now"] = False
        await asyncio.sleep(0.1)  # the scheduled recheck re-probes /health
        assert upstream.healthy is True
        assert [u.name for u in router.eligible()] == ["ff1", "chrome"]


@pytest.mark.asyncio
async def test_unhealthy_upstream_is_skipped_and_rejoins_at_its_config_position(sessions_path):
    a = ScriptedUpstream("ff1")
    b = ScriptedUpstream("chrome", connected=False)
    c = ScriptedUpstream("ff2")

    async with router_with([a, b, c], sessions_path, health_interval_seconds=3600) as router:
        first = await router.forward_prompt(PromptRequest(text="1"))
        second = await router.forward_prompt(PromptRequest(text="2"))
        assert [first.upstream, second.upstream] == ["ff1", "ff2"]

        b.connected = True
        await router._health_sweep()
        order = [
            (await router.forward_prompt(PromptRequest(text=str(i)))).upstream for i in range(3)
        ]

    assert set(order) == {"ff1", "chrome", "ff2"}


# --------------------------------------------------------------------------- #
# /upstreams
# --------------------------------------------------------------------------- #


class _FakeFront:
    def __init__(self, payload):
        self.payload = payload

    def snapshot(self):
        return self.payload


class _BrokenFront:
    def snapshot(self):
        raise RuntimeError("front is on fire")


@pytest.mark.asyncio
async def test_upstreams_endpoint_reports_state_and_fronts(sessions_path, monkeypatch):
    from ai_prompt_router import state as router_state

    stubs = [ScriptedUpstream("ff1", providers=["chatgpt"]), ScriptedUpstream("chrome", enabled=False)]
    async with router_with(stubs, sessions_path) as router:
        router.sessions.record("sess_x", "ff1")
        monkeypatch.setattr(
            router_state, "ws_front", _FakeFront([{"url": "ws://127.0.0.1:8760", "connected": True}])
        )
        monkeypatch.setattr(
            router_state, "unix_front", _FakeFront({"path": "/tmp/ai-prompt-router.sock"})
        )
        async with router_client(router) as client:
            body = (await client.get("/upstreams")).json()

    assert body["router"]["strategy"] == "round_robin"
    ff1 = body["upstreams"][0]
    assert ff1["name"] == "ff1"
    assert ff1["healthy"] is True
    assert ff1["sessions_owned"] == 1
    assert ff1["in_flight"] == 0
    assert ff1["last_check"] is not None
    assert body["upstreams"][1]["enabled"] is False
    assert body["fronts"]["websocket"][0]["connected"] is True
    assert body["fronts"]["unix_socket"]["path"] == "/tmp/ai-prompt-router.sock"


@pytest.mark.asyncio
async def test_upstreams_endpoint_tolerates_missing_or_broken_fronts(sessions_path, monkeypatch):
    from ai_prompt_router import state as router_state

    async with router_with([ScriptedUpstream("ff1")], sessions_path) as router:
        async with router_client(router) as client:
            without = (await client.get("/upstreams")).json()
            monkeypatch.setattr(router_state, "ws_front", _BrokenFront())
            broken = (await client.get("/upstreams")).json()

    assert without["fronts"] == {"websocket": [], "unix_socket": None}
    assert "on fire" in broken["fronts"]["websocket"]["error"]


# --------------------------------------------------------------------------- #
# Integration: real ai_prompt_api bridges behind a real router
# --------------------------------------------------------------------------- #


class LazyASGITransport(httpx.AsyncBaseTransport):
    """Resolves its target app on first use.

    The router→router chain is a cycle (A's upstream is B, B's upstream is A), so at
    least one transport has to be built before the app it points at exists.
    """

    def __init__(self, get_app):
        self._get_app = get_app
        self._inner = None

    async def handle_async_request(self, request):
        if self._inner is None:
            self._inner = httpx.ASGITransport(app=self._get_app())
        return await self._inner.handle_async_request(request)


def bridge_router(bridges: dict, sessions_path, **overrides) -> Router:
    """A Router whose upstreams are real ai_prompt_api apps driven by stub extensions."""
    transports = {
        f"http://{name}.test": asgi_transport(make_bridge_app(stub))
        for name, stub in bridges.items()
    }
    config = RouterConfig(
        upstreams=[UpstreamConfig(name=name, url=f"http://{name}.test") for name in bridges],
        sessions_path=sessions_path,
        **overrides,
    )
    return Router(config, transport_factory=lambda upstream: transports.get(upstream.url))


@pytest.mark.asyncio
async def test_integration_alternates_between_two_real_bridges(sessions_path):
    bridges = {"ff1": StubExtension("ff1"), "chrome": StubExtension("chrome")}
    router = bridge_router(bridges, sessions_path)

    async with running(router):
        async with router_client(router) as client:
            responses = [await client.post("/prompt", json={"text": f"q{i}"}) for i in range(4)]

    assert [r.headers["X-AI-Prompt-Upstream"] for r in responses] == [
        "ff1",
        "chrome",
        "ff1",
        "chrome",
    ]
    assert responses[0].json()["text"] == "ff1: q0"
    assert len(bridges["ff1"].prompts) == 2


@pytest.mark.asyncio
async def test_integration_follow_up_stays_on_the_creating_bridge(sessions_path):
    bridges = {"ff1": StubExtension("ff1"), "chrome": StubExtension("chrome")}
    router = bridge_router(bridges, sessions_path)

    async with running(router):
        async with router_client(router) as client:
            first = await client.post("/prompt", json={"text": "hello"})
            session_id = first.json()["session_id"]
            follow_up = await client.post(
                "/prompt", json={"text": "again", "session_id": session_id}
            )
            listed = await client.get("/sessions")

    assert follow_up.headers["X-AI-Prompt-Upstream"] == first.headers["X-AI-Prompt-Upstream"]
    assert bridges["ff1"].prompts[-1]["session_id"] == session_id
    assert session_id in [item["session_id"] for item in listed.json()["sessions"]]


@pytest.mark.asyncio
async def test_integration_fails_over_when_a_bridge_reports_no_extension(sessions_path):
    """The 503 comes from the real prompts.py, not from a hand-written fake."""
    bridges = {"ff1": StubExtension("ff1"), "chrome": StubExtension("chrome")}
    # Long interval so the poller does not quietly remove ff1 before the request runs:
    # the point is to exercise the failover path, not the eligibility filter.
    router = bridge_router(bridges, sessions_path, health_interval_seconds=3600)

    async with running(router):
        bridges["ff1"].is_connected = False
        async with router_client(router) as client:
            response = await client.post("/prompt", json={"text": "hi"})

    assert response.status_code == 200
    assert response.headers["X-AI-Prompt-Upstream"] == "chrome"
    assert response.headers["X-AI-Prompt-Attempts"] == "ff1,chrome"
    assert response.json()["text"] == "chrome: hi"


@pytest.mark.asyncio
async def test_integration_three_level_chain(sessions_path, tmp_path):
    """app -> router A -> router B -> bridge, with A authenticating to B."""
    bridges = {"ff1": StubExtension("ff1"), "chrome": StubExtension("chrome")}
    router_b = bridge_router(
        bridges,
        tmp_path / "b-sessions.json",
        api_keys=[{"name": "router-a", "key": "chain-key"}],
    )
    app_b = make_router_app(router_b)

    config_a = RouterConfig(
        upstreams=[UpstreamConfig(name="router-b", url="http://router-b.test", api_key="chain-key")],
        sessions_path=sessions_path,
    )
    router_a = Router(config_a, transport_factory=lambda upstream: asgi_transport(app_b))

    async with running(router_b), running(router_a):
        async with router_client(router_a) as client:
            first = await client.post("/prompt", json={"text": "hello"})
            session_id = first.json()["session_id"]
            follow_up = await client.post(
                "/prompt", json={"text": "again", "session_id": session_id}
            )

    assert first.status_code == 200
    assert first.headers["X-AI-Prompt-Upstream"] == "router-b"
    assert first.json()["text"] == "ff1: hello"
    # Affinity survives the chain: A pins to B, B pins to the bridge that has the tab.
    assert follow_up.json()["text"] == "ff1: again"
    assert bridges["chrome"].prompts == []


@pytest.mark.asyncio
async def test_integration_detects_a_router_cycle(sessions_path, tmp_path):
    """A -> B -> A: the second visit to A trips the Via guard and 508 propagates back."""
    apps: dict = {}

    router_a = Router(
        RouterConfig(
            upstreams=[UpstreamConfig(name="router-b", url="http://router-b.test")],
            sessions_path=sessions_path,
        ),
        transport_factory=lambda upstream: LazyASGITransport(lambda: apps["b"]),
    )
    router_b = Router(
        RouterConfig(
            upstreams=[UpstreamConfig(name="router-a", url="http://router-a.test")],
            sessions_path=tmp_path / "b-sessions.json",
        ),
        transport_factory=lambda upstream: LazyASGITransport(lambda: apps["a"]),
    )
    apps["a"] = make_router_app(router_a)
    apps["b"] = make_router_app(router_b)

    async with running(router_a), running(router_b):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=apps["a"]), base_url="http://router-a.test"
        ) as client:
            response = await client.post("/prompt", json={"text": "hi"})

    assert response.status_code == 508


@pytest.mark.asyncio
async def test_integration_all_bridges_down_returns_the_bridge_503(sessions_path):
    bridges = {"ff1": StubExtension("ff1", connected=False), "chrome": StubExtension("chrome", connected=False)}
    router = bridge_router(bridges, sessions_path)

    async with running(router):
        async with router_client(router) as client:
            prompt = await client.post("/prompt", json={"text": "hi"})
            health = await client.get("/health")

    assert prompt.status_code == 503
    assert prompt.json()["detail"] == "Browser extension not connected"
    assert health.json()["extension_connected"] is False
