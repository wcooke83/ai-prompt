"""Phase 1: config loading, eligibility, rotation, hop guard, the HTTP front."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import (
    ScriptedUpstream,
    build_router,
    router_client,
    router_with,
    running,
)

from ai_prompt_api.models import Attachment, PromptRequest
from ai_prompt_router.config import ENV_CONFIG, ENV_PORT, RouterConfig, load_config
from ai_prompt_router.router import LoopDetected, NoUpstreamAvailable, Router, parse_hops, parse_via
from ai_prompt_router.strategy import RoundRobin, make_strategy

MINIMAL = {
    "listen": {"host": "127.0.0.1", "port": 8300},
    "upstreams": [
        {"name": "ff1", "url": "http://127.0.0.1:8201", "api_key": "a", "providers": ["chatgpt"]},
        {"name": "chrome", "url": "http://127.0.0.1:8202/", "api_key": "b"},
    ],
}


def write_config(tmp_path: Path, payload: dict, name: str = "router.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_load_config_reads_file_and_applies_defaults(tmp_path):
    config = load_config(write_config(tmp_path, MINIMAL))

    assert config.listen.port == 8300
    assert [u.name for u in config.upstreams] == ["ff1", "chrome"]
    # trailing slash normalised so base_url joins and /upstreams output are stable
    assert config.upstreams[1].url == "http://127.0.0.1:8202"
    assert config.strategy == "round_robin"
    assert config.failover is True
    assert config.max_attempts == 3
    assert config.max_hops == 4
    assert config.unix_socket is not None
    assert config.unix_socket.path == "/tmp/ai-prompt-router.sock"


def test_load_config_precedence_argument_over_env(tmp_path, monkeypatch):
    from_env = write_config(tmp_path, MINIMAL, "env.json")
    explicit = write_config(
        tmp_path,
        {**MINIMAL, "listen": {"port": 9999}},
        "explicit.json",
    )
    monkeypatch.setenv(ENV_CONFIG, str(from_env))

    assert load_config(explicit).listen.port == 9999
    assert load_config().listen.port == 8300  # falls back to $AI_PROMPT_ROUTER_CONFIG


def test_env_port_override(tmp_path, monkeypatch):
    path = write_config(tmp_path, MINIMAL)
    monkeypatch.setenv(ENV_PORT, "8500")
    assert load_config(path).listen.port == 8500


def test_env_port_must_be_an_integer(tmp_path, monkeypatch):
    path = write_config(tmp_path, MINIMAL)
    monkeypatch.setenv(ENV_PORT, "eight-thousand")
    with pytest.raises(ValueError, match=ENV_PORT):
        load_config(path)


def test_unix_socket_can_be_disabled(tmp_path):
    config = load_config(write_config(tmp_path, {**MINIMAL, "unix_socket": None}))
    assert config.unix_socket is None


@pytest.mark.parametrize(
    "payload, message",
    [
        (
            {"upstreams": [{"name": "a", "url": "http://x"}, {"name": "a", "url": "http://y"}]},
            "duplicate upstream name",
        ),
        ({"upstreams": []}, "at least one upstream"),
        ({"upstreams": [{"name": "a", "url": "127.0.0.1:8201"}]}, "must start with"),
        ({"upstreams": [{"name": "a", "url": "http://"}]}, "missing a host"),
        (
            {"upstreams": [{"name": "a", "url": "http://x", "providers": ["gemini"]}]},
            "unknown provider",
        ),
        (
            {"strategy": "least_busy", "upstreams": [{"name": "a", "url": "http://x"}]},
            "unknown strategy",
        ),
        ({"upstreams": [{"name": "a", "url": "http://x", "typo": 1}]}, "typo"),
    ],
)
def test_config_validation_errors(tmp_path, payload, message):
    with pytest.raises(ValueError, match=message):
        load_config(write_config(tmp_path, payload))


def test_missing_config_file_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        load_config(tmp_path / "nope.json")


def test_malformed_json_is_a_clear_error(tmp_path):
    path = tmp_path / "router.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_config(path)


# --------------------------------------------------------------------------- #
# Strategy
# --------------------------------------------------------------------------- #


def test_round_robin_rotates_and_returns_the_full_order():
    strategy = RoundRobin()
    assert strategy.order(["a", "b", "c"]) == ["a", "b", "c"]
    assert strategy.order(["a", "b", "c"]) == ["b", "c", "a"]
    assert strategy.order(["a", "b", "c"]) == ["c", "a", "b"]
    assert strategy.order(["a", "b", "c"]) == ["a", "b", "c"]
    assert strategy.order([]) == []


def test_make_strategy_rejects_unknown_names():
    assert isinstance(make_strategy("round_robin"), RoundRobin)
    with pytest.raises(ValueError, match="unknown strategy"):
        make_strategy("least_busy")


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #


def test_eligibility_filters_disabled_unhealthy_and_misconfigured(sessions_path):
    router = build_router(
        [
            ScriptedUpstream("ok"),
            ScriptedUpstream("off", enabled=False),
            ScriptedUpstream("down"),
            ScriptedUpstream("badkey"),
        ],
        sessions_path,
    )
    router._by_name["down"].healthy = False
    router._by_name["badkey"].misconfigured = True

    assert [u.name for u in router.eligible()] == ["ok"]
    assert router.any_healthy() is True


def test_pin_filtering_uses_the_static_provider_list(sessions_path):
    router = build_router(
        [
            ScriptedUpstream("ff1", providers=["chatgpt", "grok"]),
            ScriptedUpstream("chrome", providers=["claude"]),
            ScriptedUpstream("any"),  # no providers list = assume every provider
        ],
        sessions_path,
    )

    assert [u.name for u in router.eligible(None)] == ["ff1", "chrome", "any"]
    assert [u.name for u in router.eligible("grok")] == ["ff1", "any"]
    assert [u.name for u in router.eligible("claude")] == ["chrome", "any"]
    assert [u.name for u in router.eligible("deepseek")] == ["any"]


def test_available_providers_is_the_union_of_configured_lists(sessions_path):
    narrow = build_router(
        [ScriptedUpstream("a", providers=["chatgpt"]), ScriptedUpstream("b", providers=["grok"])],
        sessions_path,
    )
    assert narrow.available_providers() == ["chatgpt", "grok"]

    # One "assume everything" upstream widens the union to the whole enum.
    wide = build_router(
        [ScriptedUpstream("a", providers=["chatgpt"]), ScriptedUpstream("b")], sessions_path
    )
    assert wide.available_providers() == ["chatgpt", "claude", "grok", "deepseek"]


# --------------------------------------------------------------------------- #
# Forwarding
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_round_robin_alternates_between_upstreams(sessions_path):
    stubs = [ScriptedUpstream("ff1"), ScriptedUpstream("chrome")]
    async with router_with(stubs, sessions_path) as router:
        seen = [
            (await router.forward_prompt(PromptRequest(text=f"q{i}"))).upstream for i in range(4)
        ]
    assert seen == ["ff1", "chrome", "ff1", "chrome"]


@pytest.mark.asyncio
async def test_body_is_forwarded_verbatim(sessions_path):
    stub = ScriptedUpstream("ff1")
    request = PromptRequest(
        text="hello",
        provider="grok",
        timeout=30,
        ephemeral=True,
        attachments=[Attachment(name="a.txt", type="text/plain", data="aGk=")],
    )
    async with router_with([stub], sessions_path) as router:
        await router.forward_prompt(request)

    assert stub.prompt_bodies[0] == {
        "text": "hello",
        "provider": "grok",
        "timeout": 30.0,
        "ephemeral": True,
        "attachments": [{"name": "a.txt", "type": "text/plain", "data": "aGk="}],
    }
    # session_id / images were None and must not appear as nulls
    assert "session_id" not in stub.prompt_bodies[0]


@pytest.mark.asyncio
async def test_response_is_returned_unchanged(sessions_path):
    stub = ScriptedUpstream("ff1")
    async with router_with([stub], sessions_path) as router:
        result = await router.forward_prompt(PromptRequest(text="hello"))

    assert result.response.success is True
    assert result.response.text == "ff1: hello"
    assert result.attempts == ["ff1"]


@pytest.mark.asyncio
async def test_no_eligible_upstream_raises(sessions_path):
    async with router_with([ScriptedUpstream("off", enabled=False)], sessions_path) as router:
        with pytest.raises(NoUpstreamAvailable):
            await router.forward_prompt(PromptRequest(text="hi"))


@pytest.mark.asyncio
async def test_pinned_provider_with_no_matching_upstream_raises(sessions_path):
    async with router_with([ScriptedUpstream("ff1", providers=["chatgpt"])], sessions_path) as router:
        with pytest.raises(NoUpstreamAvailable, match="grok"):
            await router.forward_prompt(PromptRequest(text="hi", provider="grok"))


# --------------------------------------------------------------------------- #
# Hop guard
# --------------------------------------------------------------------------- #


def test_hop_header_parsing_is_defensive():
    assert parse_hops(None) == 0
    assert parse_hops("2") == 2
    assert parse_hops("nonsense") == 0
    assert parse_hops("-3") == 0
    assert parse_via(None) == []
    assert parse_via("a, b ,,c") == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_hop_guard_trips_at_max_hops(sessions_path):
    async with router_with([ScriptedUpstream("ff1")], sessions_path, max_hops=2) as router:
        await router.forward_prompt(PromptRequest(text="hi"), hops=1)  # still under the limit
        with pytest.raises(LoopDetected, match="max_hops"):
            await router.forward_prompt(PromptRequest(text="hi"), hops=2)


@pytest.mark.asyncio
async def test_hop_guard_trips_when_our_own_id_is_in_via(sessions_path):
    stub = ScriptedUpstream("ff1")
    async with router_with([stub], sessions_path) as router:
        with pytest.raises(LoopDetected, match=router.instance_id):
            await router.forward_prompt(
                PromptRequest(text="hi"), hops=1, via=["router-other", router.instance_id]
            )
    assert stub.calls["prompt"] == 0


@pytest.mark.asyncio
async def test_forwarded_headers_increment_hops_and_append_via(sessions_path):
    stub = ScriptedUpstream("ff1")
    async with router_with([stub], sessions_path) as router:
        await router.forward_prompt(PromptRequest(text="hi"))
        await router.forward_prompt(PromptRequest(text="hi"), hops=1, via=["router-upstream"])

    assert stub.prompt_headers[0]["x-ai-prompt-hops"] == "1"
    assert stub.prompt_headers[0]["x-ai-prompt-via"] == router.instance_id
    assert stub.prompt_headers[1]["x-ai-prompt-hops"] == "2"
    assert stub.prompt_headers[1]["x-ai-prompt-via"] == f"router-upstream,{router.instance_id}"


# --------------------------------------------------------------------------- #
# HTTP front
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_prompt_endpoint_reports_the_upstream_it_used(sessions_path):
    stubs = [ScriptedUpstream("ff1"), ScriptedUpstream("chrome")]
    async with router_with(stubs, sessions_path) as router:
        async with router_client(router) as client:
            first = await client.post("/prompt", json={"text": "one"})
            second = await client.post("/prompt", json={"text": "two"})

    assert first.status_code == 200
    assert first.json()["text"] == "ff1: one"
    assert first.headers["X-AI-Prompt-Upstream"] == "ff1"
    assert second.headers["X-AI-Prompt-Upstream"] == "chrome"


@pytest.mark.asyncio
async def test_health_and_status_endpoints(sessions_path):
    stubs = [ScriptedUpstream("ff1", providers=["chatgpt"]), ScriptedUpstream("chrome")]
    async with router_with(stubs, sessions_path) as router:
        async with router_client(router) as client:
            health = await client.get("/health")
            status = await client.get("/status")

    assert health.json() == {"status": "ok", "extension_connected": True, "active_provider": None}
    body = status.json()
    assert body["extension_connected"] is True
    assert body["pending_requests"] == 0
    assert body["active_sessions"] == 0
    assert set(body["available_providers"]) == {"chatgpt", "claude", "grok", "deepseek"}


@pytest.mark.asyncio
async def test_health_reports_disconnected_when_no_upstream_is_usable(sessions_path):
    async with router_with([ScriptedUpstream("ff1", connected=False)], sessions_path) as router:
        async with router_client(router) as client:
            health = await client.get("/health")
            prompt = await client.post("/prompt", json={"text": "hi"})

    assert health.json()["extension_connected"] is False
    # Same status and wording a lone bridge uses, so existing callers need no change.
    assert prompt.status_code == 503
    assert prompt.json()["detail"] == "Browser extension not connected"


@pytest.mark.asyncio
async def test_loop_detection_maps_to_508(sessions_path):
    async with router_with([ScriptedUpstream("ff1")], sessions_path) as router:
        async with router_client(router) as client:
            response = await client.post(
                "/prompt", json={"text": "hi"}, headers={"X-AI-Prompt-Via": router.instance_id}
            )
    assert response.status_code == 508


@pytest.mark.asyncio
async def test_api_keys_gate_the_http_front_and_are_echoed(sessions_path):
    api_keys = [{"name": "securities", "key": "sec-key"}, {"name": "dev", "key": "dev-key"}]
    async with router_with([ScriptedUpstream("ff1")], sessions_path, api_keys=api_keys) as router:
        async with router_client(router) as client:
            anonymous = await client.post("/prompt", json={"text": "hi"})
            wrong = await client.post(
                "/prompt", json={"text": "hi"}, headers={"Authorization": "Bearer nope"}
            )
            allowed = await client.post(
                "/prompt", json={"text": "hi"}, headers={"Authorization": "Bearer dev-key"}
            )
            upstreams = await client.get(
                "/upstreams", headers={"Authorization": "Bearer sec-key"}
            )
            # /health stays open: it is what a parent router and monitoring poll.
            health = await client.get("/health")

    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert allowed.status_code == 200
    assert allowed.headers["X-AI-Prompt-Client"] == "dev"
    assert upstreams.status_code == 200
    assert upstreams.headers["X-AI-Prompt-Client"] == "securities"
    assert health.status_code == 200


@pytest.mark.asyncio
async def test_no_auth_configured_means_open_front(sessions_path):
    async with router_with([ScriptedUpstream("ff1")], sessions_path) as router:
        async with router_client(router) as client:
            response = await client.post("/prompt", json={"text": "hi"})
    assert response.status_code == 200
    assert "X-AI-Prompt-Client" not in response.headers


@pytest.mark.asyncio
async def test_prompt_request_contract_is_the_bridge_contract(sessions_path):
    """The router validates exactly what the bridge validates (shared models)."""
    async with router_with([ScriptedUpstream("ff1")], sessions_path) as router:
        async with router_client(router) as client:
            bad_provider = await client.post("/prompt", json={"text": "hi", "provider": "gemini"})
            empty_text = await client.post("/prompt", json={"text": ""})
    assert bad_provider.status_code == 422
    assert empty_text.status_code == 422


@pytest.mark.asyncio
async def test_router_without_a_wired_router_answers_503(sessions_path):
    from ai_prompt_router.app import create_app

    import httpx

    app = create_app()  # no router bound, and state.router is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://router.test"
    ) as client:
        prompt = await client.post("/prompt", json={"text": "hi"})
        health = await client.get("/health")
    assert prompt.status_code == 503
    assert health.json()["extension_connected"] is False
