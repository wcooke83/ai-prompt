"""Phase 2: session affinity, fan-out resolution, persistence, /sessions routes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import (
    ScriptedUpstream,
    build_router,
    respond_failure,
    respond_status,
    router_client,
    router_with,
    running,
)

from ai_prompt_api.models import PromptRequest
from ai_prompt_router.router import NoUpstreamAvailable
from ai_prompt_router.sessions import SessionMap


def hours_ago(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


# --------------------------------------------------------------------------- #
# The map itself
# --------------------------------------------------------------------------- #


def test_session_map_round_trips_through_disk(sessions_path):
    original = SessionMap(sessions_path)
    original.record("sess_a", "ff1", "chatgpt")
    original.record("sess_b", "chrome", "grok")

    assert sessions_path.exists()
    on_disk = json.loads(sessions_path.read_text())
    assert on_disk["sess_a"]["upstream"] == "ff1"
    assert on_disk["sess_b"]["provider"] == "grok"

    reloaded = SessionMap(sessions_path)
    reloaded.load()
    assert reloaded.owner("sess_a") == "ff1"
    assert reloaded.owner("sess_b") == "chrome"
    assert len(reloaded) == 2


def test_session_map_writes_atomically_and_leaves_no_temp_files(sessions_path):
    smap = SessionMap(sessions_path)
    smap.record("sess_a", "ff1")
    smap.record("sess_b", "ff1")
    leftovers = [p.name for p in sessions_path.parent.iterdir() if p.name != sessions_path.name]
    assert leftovers == []


def test_session_map_skips_the_write_when_nothing_changed(sessions_path, monkeypatch):
    smap = SessionMap(sessions_path)
    smap.record("sess_a", "ff1", "chatgpt")

    writes = []
    monkeypatch.setattr(SessionMap, "save", lambda self: writes.append(1))
    smap.record("sess_a", "ff1", "chatgpt")  # a follow-up prompt re-records the same pair
    assert writes == []
    smap.record("sess_a", "chrome", "chatgpt")  # a real change still persists
    assert writes == [1]


def test_session_map_prunes_by_age(sessions_path):
    smap = SessionMap(sessions_path, ttl_hours=24)
    smap.record("fresh", "ff1", created_at=hours_ago(1))
    smap.record("stale", "ff1", created_at=hours_ago(25))

    assert smap.get("stale") is None  # expired entries are dropped on read
    assert smap.owner("fresh") == "ff1"
    assert len(smap) == 1

    reloaded = SessionMap(sessions_path, ttl_hours=24)
    reloaded.load()
    assert list(reloaded.ids()) == ["fresh"]


def test_session_map_survives_a_corrupt_file(sessions_path):
    sessions_path.write_text("{ this is not json", encoding="utf-8")
    smap = SessionMap(sessions_path)
    smap.load()
    assert len(smap) == 0
    smap.record("sess_a", "ff1")
    assert smap.owner("sess_a") == "ff1"


def test_session_map_counts_ownership_per_upstream(sessions_path):
    smap = SessionMap(sessions_path)
    smap.record("a", "ff1")
    smap.record("b", "ff1")
    smap.record("c", "chrome")
    assert smap.owned_by("ff1") == 2
    assert smap.owned_by("chrome") == 1
    assert smap.forget("a") is True
    assert smap.forget("a") is False
    assert smap.owned_by("ff1") == 1


# --------------------------------------------------------------------------- #
# Affinity
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_follow_ups_stick_to_the_creating_upstream(sessions_path):
    stubs = [ScriptedUpstream("ff1"), ScriptedUpstream("chrome")]
    async with router_with(stubs, sessions_path) as router:
        first = await router.forward_prompt(PromptRequest(text="one"))
        session_id = first.response.session_id

        follow_ups = [
            (await router.forward_prompt(PromptRequest(text="more", session_id=session_id))).upstream
            for _ in range(3)
        ]

    assert first.upstream == "ff1"
    # Rotation would have alternated; affinity wins because the tab only exists on ff1.
    assert follow_ups == ["ff1", "ff1", "ff1"]
    assert stubs[1].calls["prompt"] == 0
    assert stubs[0].prompt_bodies[-1]["session_id"] == session_id


@pytest.mark.asyncio
async def test_new_conversation_owner_is_recorded_and_persisted(sessions_path):
    async with router_with([ScriptedUpstream("ff1")], sessions_path) as router:
        result = await router.forward_prompt(PromptRequest(text="hi"))
        session_id = result.response.session_id
        assert router.sessions.owner(session_id) == "ff1"

    reloaded = SessionMap(sessions_path)
    reloaded.load()
    assert reloaded.owner(session_id) == "ff1"


@pytest.mark.asyncio
async def test_a_known_owner_never_fails_over(sessions_path):
    owner = ScriptedUpstream("ff1", prompt=respond_failure("Extension not connected"))
    other = ScriptedUpstream("chrome")
    router = build_router([owner, other], sessions_path)
    router.sessions.record("sess_x", "ff1")

    async with running(router):
        result = await router.forward_prompt(PromptRequest(text="hi", session_id="sess_x"))

    assert result.attempts == ["ff1"]
    assert result.response.success is False
    assert other.calls["prompt"] == 0


@pytest.mark.asyncio
async def test_a_dead_owner_does_not_leak_the_conversation_elsewhere(sessions_path):
    owner = ScriptedUpstream("ff1", prompt=respond_status(503))
    other = ScriptedUpstream("chrome")
    router = build_router([owner, other], sessions_path)
    router.sessions.record("sess_x", "ff1")

    async with running(router):
        with pytest.raises(NoUpstreamAvailable):
            await router.forward_prompt(PromptRequest(text="hi", session_id="sess_x"))

    assert other.calls["prompt"] == 0


@pytest.mark.asyncio
async def test_unknown_session_is_resolved_by_fan_out(sessions_path):
    a = ScriptedUpstream("ff1")
    b = ScriptedUpstream("chrome")
    b.add_session("sess_from_before_the_router", provider="grok")

    async with router_with([a, b], sessions_path) as router:
        result = await router.forward_prompt(
            PromptRequest(text="hi", session_id="sess_from_before_the_router")
        )

    assert result.upstream == "chrome"
    assert router.sessions.owner("sess_from_before_the_router") == "chrome"
    assert a.calls["get_session"] == 1 and b.calls["get_session"] == 1
    assert a.calls["prompt"] == 0


@pytest.mark.asyncio
async def test_unresolvable_session_becomes_a_new_conversation(sessions_path):
    """Mirrors the extension: an unknown session id silently starts a new session."""
    a = ScriptedUpstream("ff1")
    b = ScriptedUpstream("chrome")
    async with router_with([a, b], sessions_path) as router:
        result = await router.forward_prompt(PromptRequest(text="hi", session_id="sess_ghost"))

    assert result.upstream == "ff1"  # back to plain rotation
    assert a.calls["prompt"] == 1


@pytest.mark.asyncio
async def test_owner_that_left_the_config_is_forgotten(sessions_path):
    stub = ScriptedUpstream("ff1")
    router = build_router([stub], sessions_path)
    router.sessions.record("sess_x", "retired-browser")

    async with running(router):
        result = await router.forward_prompt(PromptRequest(text="hi", session_id="sess_x"))

    assert result.upstream == "ff1"
    assert router.sessions.owner("sess_x") == "ff1"


@pytest.mark.asyncio
async def test_expired_mapping_is_re_resolved(sessions_path):
    stub = ScriptedUpstream("ff1")
    stub.add_session("sess_x")
    router = build_router([stub], sessions_path, session_ttl_hours=1)
    router.sessions.record("sess_x", "ff1", created_at=hours_ago(5))

    assert router.sessions.owner("sess_x") is None  # aged out of the map

    async with running(router):
        result = await router.forward_prompt(PromptRequest(text="hi", session_id="sess_x"))

    # The bridge still has the tab, so the fan-out finds it again.
    assert result.upstream == "ff1"
    assert router.sessions.owner("sess_x") == "ff1"


# --------------------------------------------------------------------------- #
# /sessions routes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_sessions_merges_and_dedupes(sessions_path):
    a = ScriptedUpstream("ff1")
    b = ScriptedUpstream("chrome")
    a.add_session("sess_1")
    a.add_session("shared")
    b.add_session("sess_2")
    b.add_session("shared")

    async with router_with([a, b], sessions_path) as router:
        async with router_client(router) as client:
            response = await client.get("/sessions")

    ids = sorted(item["session_id"] for item in response.json()["sessions"])
    assert ids == ["sess_1", "sess_2", "shared"]
    # Listing also teaches the map who owns what, sparing a later fan-out.
    assert router.sessions.owner("sess_2") == "chrome"
    assert router.sessions.owner("shared") == "ff1"


@pytest.mark.asyncio
async def test_get_session_uses_the_map_then_falls_back_to_fan_out(sessions_path):
    a = ScriptedUpstream("ff1")
    b = ScriptedUpstream("chrome")
    b.add_session("sess_x", provider="grok")

    async with router_with([a, b], sessions_path) as router:
        async with router_client(router) as client:
            found = await client.get("/sessions/sess_x")
            missing = await client.get("/sessions/sess_nope")

    assert found.status_code == 200
    assert found.json()["provider"] == "grok"
    assert missing.status_code == 404
    assert router.sessions.owner("sess_x") == "chrome"


@pytest.mark.asyncio
async def test_a_404_from_the_recorded_owner_prunes_the_mapping(sessions_path):
    a = ScriptedUpstream("ff1")  # the map claims ff1 owns it, but ff1 has no such tab
    b = ScriptedUpstream("chrome")
    b.add_session("sess_x")
    router = build_router([a, b], sessions_path)
    router.sessions.record("sess_x", "ff1")

    async with running(router):
        info = await router.get_session("sess_x")

    assert info is not None
    # Re-pointed at the upstream that actually has it, not left pinned at ff1.
    assert router.sessions.owner("sess_x") == "chrome"


@pytest.mark.asyncio
async def test_delete_session_drops_the_mapping(sessions_path):
    stub = ScriptedUpstream("ff1")
    stub.add_session("sess_x")
    router = build_router([stub], sessions_path)
    router.sessions.record("sess_x", "ff1")

    async with running(router):
        async with router_client(router) as client:
            deleted = await client.delete("/sessions/sess_x")
            again = await client.delete("/sessions/sess_x")

    assert deleted.status_code == 200
    assert deleted.json() == {"success": True}
    assert again.status_code == 404
    assert router.sessions.owner("sess_x") is None
    assert json.loads(sessions_path.read_text()) == {}
