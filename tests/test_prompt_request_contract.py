#!/usr/bin/env python3
"""Pin the provider round-trip contract: models.py -> router transform -> service payload.

Stdlib + the deps already in the API venv (pydantic, websockets). No pytest / httpx
(the repo has neither), so this is a plain runnable script:

    ai_prompt_api/.venv/bin/python tests/test_prompt_request_contract.py

What it locks in:
  1. PromptRequest validates `provider` as the Provider enum and rejects unknown values,
     so only {chatgpt,claude,grok,deepseek} or None can ever reach the extension.
  2. The exact transform the router applies -- `request.provider.value if request.provider
     else None` (routers/prompts.py:31) -- yields the provider string when pinned and None
     when unpinned.
  3. ExtensionManager.send_prompt puts that string into message["provider"] when pinned,
     and OMITS the key entirely when unpinned. background.js reads message.provider as
     `preferredProvider`; a present value is a TRUE PIN, an absent value (undefined -> null)
     is the unchanged full-failover path.
"""
import asyncio
import json
import os
import sys

# Import the API package from the repo root regardless of cwd.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError

from ai_prompt_api.models import PromptRequest, Provider
from ai_prompt_api.services.extension import ExtensionManager

FAILURES = []


def check(name, fn):
    try:
        fn()
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 - test harness
        FAILURES.append(name)
        print(f"  FAIL  {name}\n        {type(e).__name__}: {e}")


# --- 1. model validation + router transform ------------------------------------
def router_provider_arg(req: PromptRequest):
    """Mirror routers/prompts.py:31 exactly."""
    return req.provider.value if req.provider else None


def test_pinned_provider_validates_and_round_trips():
    req = PromptRequest(text="hi", provider="grok")
    assert req.provider is Provider.grok, req.provider
    assert router_provider_arg(req) == "grok", router_provider_arg(req)


def test_all_enum_providers_round_trip():
    for name in ("chatgpt", "claude", "grok", "deepseek"):
        req = PromptRequest(text="hi", provider=name)
        assert router_provider_arg(req) == name, (name, router_provider_arg(req))


def test_unpinned_request_transforms_to_none():
    req = PromptRequest(text="hi")  # no provider
    assert req.provider is None
    assert router_provider_arg(req) is None


def test_unknown_provider_is_rejected():
    try:
        PromptRequest(text="hi", provider="gemini")
    except ValidationError:
        return
    raise AssertionError("unknown provider 'gemini' was accepted but must be rejected")


# --- 2. service payload construction (the message the extension reads) ----------
class _FakeClient:
    """Stand-in for the connected extension websocket.

    On send(), it records the payload and immediately resolves the matching pending
    future with a canned success response, so send_prompt() returns without a real socket.
    """

    def __init__(self, manager):
        self.manager = manager
        self.sent = None

    async def send(self, data):
        self.sent = json.loads(data)
        rid = self.sent["request_id"]
        fut = self.manager.pending_requests.get(rid)
        if fut and not fut.done():
            fut.set_result(
                {"success": True, "text": "ok", "provider": self.sent.get("provider"),
                 "session_id": None}
            )


def _send(provider):
    async def run():
        mgr = ExtensionManager()
        mgr.client = _FakeClient(mgr)
        await mgr.send_prompt(text="hi", provider=provider, timeout=5)
        return mgr.client.sent

    return asyncio.run(run())


def test_pinned_provider_in_payload():
    sent = _send("grok")
    assert sent.get("provider") == "grok", sent


def test_unpinned_omits_provider_key():
    sent = _send(None)
    assert "provider" not in sent, f"unpinned request must omit provider key, got: {sent}"


def main():
    for name, fn in [
        ("PromptRequest pins provider and router forwards its value", test_pinned_provider_validates_and_round_trips),
        ("all four enum providers round-trip through the router transform", test_all_enum_providers_round_trip),
        ("unpinned request -> router forwards None", test_unpinned_request_transforms_to_none),
        ("unknown provider is rejected by the model", test_unknown_provider_is_rejected),
        ("pinned provider lands in the extension message payload", test_pinned_provider_in_payload),
        ("unpinned request omits provider key (full-failover path)", test_unpinned_omits_provider_key),
    ]:
        check(name, fn)

    print("")
    if FAILURES:
        print(f"{len(FAILURES)} test(s) FAILED")
        sys.exit(1)
    print("all tests passed")


if __name__ == "__main__":
    main()
